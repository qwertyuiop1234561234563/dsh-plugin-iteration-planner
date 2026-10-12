"""Agent function calling 编排循环。

流程：构造消息 → 调用 LLM → 若有 ``tool_calls`` 则并行调用内部算法引擎并回填结果 →
再次调用 LLM，直至模型给出最终文本或达到最大步数上限。

设计要点：
- 所有算法结果均来自工具（引擎）返回值，模型不得臆造；
- 单次工具失败不会中断整轮编排，而是把错误作为工具结果回填，让模型自行纠偏；
- 同一条 assistant 消息中的多个 ``tool_calls`` **并行执行**（``asyncio.gather``，保持原序回填）；
- 上下文预算超限时自动裁剪**最旧的用户回合**，保证当前请求与系统提示始终完整；
- 会话历史经 :class:`SessionStore` 持久化（默认内存 + TTL，可切 Redis），同会话串行化。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from ..errors import ApiError, llm_unavailable
from ..metrics import METRICS
from ..models import AgentRunResult, ToolCallRecord
from ..sessions import InMemorySessionStore, SessionStore
from .llm import LlmClient
from .prompts import ITERATION_PLANNER_SYSTEM_PROMPT, build_plan_instruction
from .sanitize import wrap_untrusted
from .schemas import TOOL_SPECS
from .tools import ToolDispatcher

logger = logging.getLogger("iteration_planner.agent")

#: 回填给模型的单条工具结果上限（字符），避免超大计划撑爆上下文
_MAX_TOOL_RESULT_CHARS = 60_000

#: 工具名 → 必填参数（用于执行前的结构校验）
_REQUIRED: dict[str, list[str]] = {
    spec["function"]["name"]: list(spec["function"]["parameters"].get("required", [])) for spec in TOOL_SPECS
}
_TOOL_NAMES: frozenset[str] = frozenset(_REQUIRED)


def _content_to_text(content: Any) -> str:
    """把 assistant content（str / list / None）归一化为文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "\n".join(parts)
    return str(content)


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """解析 function call 的 arguments（可能是 JSON 字符串或已是对象）。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise ApiError(422, "LLM_FORMAT_ERROR", f"工具入参 JSON 解析失败：{exc}") from exc
        if not isinstance(parsed, dict):
            raise ApiError(422, "LLM_FORMAT_ERROR", "工具入参必须是 JSON 对象")
        return parsed
    return {}


def _estimate_chars(messages: list[dict[str, Any]]) -> int:
    """估算消息列表的字符规模（约 4 字符 ≈ 1 token）。"""
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
        tool_calls = message.get("tool_calls")
        if tool_calls:
            total += len(json.dumps(tool_calls, ensure_ascii=False, default=str))
    return total


class AgentRunner:
    """无状态编排器：由 HTTP 层注入 LLM 客户端、工具调度器与会话存储。"""

    def __init__(
        self,
        llm: LlmClient | None,
        dispatcher: ToolDispatcher,
        *,
        max_steps: int = 8,
        temperature: float = 0.2,
        store: SessionStore | None = None,
        max_context_chars: int = 96_000,
    ) -> None:
        self._llm = llm
        self._dispatcher = dispatcher
        self._max_steps = max_steps
        self._temperature = temperature
        self._store: SessionStore = store or InMemorySessionStore()
        self._max_context_chars = max_context_chars

    @property
    def available(self) -> bool:
        """LLM 是否已配置（未配置时 Agent 端点给出明确错误提示）。"""
        return self._llm is not None

    # ------------------------------- 对外入口 -------------------------------

    async def run_plan(
        self,
        requirement: str,
        *,
        iteration_deadline: str | None = None,
        iteration_start_date: str | None = None,
        dev_member_count: int | None = None,
    ) -> AgentRunResult:
        """自然语言需求 → 完整计划（端到端一次编排）。"""
        instruction = build_plan_instruction(
            requirement,
            iteration_deadline=iteration_deadline,
            iteration_start_date=iteration_start_date,
            dev_member_count=dev_member_count,
        )
        messages = self._base_messages() + [{"role": "user", "content": instruction}]
        return await self._loop(messages)

    async def run_chat(self, session_id: str, message: str) -> AgentRunResult:
        """会话式对话：携带历史上下文继续编排（同会话串行化）。"""
        async with self._store.lock(session_id):
            history = await self._store.get(session_id) or []
            messages = self._base_messages() + history + [{"role": "user", "content": wrap_untrusted(message)}]
            result = await self._loop(messages)

            # 仅保留「用户 / 最终助手」两条，工具中间态不入历史，控制上下文体积
            # 历史存原始（已消毒）文本，边界标记仅用于本回合送入模型的即时输入
            updated = history + [
                {"role": "user", "content": message},
                {"role": "assistant", "content": result.content},
            ]
            await self._store.set(session_id, updated)
            return result

    async def reset_session(self, session_id: str) -> bool:
        """清空指定会话，返回是否存在。"""
        return await self._store.pop(session_id)

    async def complete(self, messages: list[dict[str, Any]], *, temperature: float | None = None) -> str:
        """单轮纯文本补全（不携带工具，供风险建议等「一次性生成」场景使用）。

        与 :meth:`_loop` 不同，这里不做 function calling 编排：只调用一次模型并把
        assistant 文本归一化返回；未配置 LLM 时抛出 :func:`llm_unavailable` 由调用方降级。
        """
        if self._llm is None:
            raise llm_unavailable()
        message = await self._llm.chat(
            messages,
            temperature=self._temperature if temperature is None else temperature,
        )
        return _content_to_text(message.get("content"))

    # ------------------------------- 内部实现 -------------------------------

    @staticmethod
    def _base_messages() -> list[dict[str, Any]]:
        return [{"role": "system", "content": ITERATION_PLANNER_SYSTEM_PROMPT}]

    async def _loop(self, messages: list[dict[str, Any]]) -> AgentRunResult:
        if self._llm is None:
            raise llm_unavailable()

        records: list[ToolCallRecord] = []
        plan_id: str | None = None

        for step in range(1, self._max_steps + 1):
            self._enforce_budget(messages)
            message = await self._llm.chat(messages, tools=TOOL_SPECS, temperature=self._temperature)
            messages.append(self._canonical_assistant(message))

            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                METRICS.observe("agent_steps", step, buckets=(1, 2, 3, 4, 5, 6, 8), help="Agent 收敛步数")
                METRICS.inc_counter("agent_runs_total", {"outcome": "completed"})
                return AgentRunResult(
                    content=_content_to_text(message.get("content")),
                    planId=plan_id,
                    toolCalls=records,
                    steps=step,
                )

            # 同一条消息内的多个工具调用并行执行，结果按原序回填（OpenAI 协议要求顺序一致）
            outcomes = await asyncio.gather(*(self._run_tool_call(index, call) for index, call in enumerate(tool_calls)))
            for record, tool_message, produced_plan_id in outcomes:
                records.append(record)
                plan_id = produced_plan_id or plan_id
                messages.append(tool_message)

        METRICS.inc_counter("agent_runs_total", {"outcome": "max_steps"})
        raise ApiError(
            422,
            "AGENT_MAX_STEPS",
            f"Agent 在 {self._max_steps} 步内未收敛，请细化需求描述或减少需变更的任务数量",
        )

    def _enforce_budget(self, messages: list[dict[str, Any]]) -> None:
        """上下文超预算时，从最旧的「用户回合」开始裁剪（system 与当前输入始终保留）。"""
        if self._max_context_chars <= 0:
            return
        trimmed = False
        while _estimate_chars(messages) > self._max_context_chars:
            user_indices = [i for i, message in enumerate(messages) if message.get("role") == "user"]
            if len(user_indices) <= 1:
                break  # 仅剩当前用户输入，无法继续裁剪
            start, end = user_indices[0], user_indices[1]
            del messages[start:end]
            trimmed = True
        if trimmed:
            METRICS.inc_counter("agent_context_trimmed_total")

    @staticmethod
    def _canonical_assistant(message: dict[str, Any]) -> dict[str, Any]:
        """规范 assistant 消息，保证 tool_calls 结构完整（OpenAI 协议要求 id 必填）。"""
        canonical: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
        tool_calls = message.get("tool_calls")
        if tool_calls:
            canonical["tool_calls"] = tool_calls
        return canonical

    async def _run_tool_call(
        self, index: int, call: Any
    ) -> tuple[ToolCallRecord, dict[str, Any], str | None]:
        """校验 + 执行单个工具调用（任何异常都以错误结果回填，绝不中断整轮编排）。

        并行执行（``asyncio.gather``）下，单个子任务抛出非预期异常会导致整批失败并中断
        Agent 循环，因此这里做**兜底捕获**：把任何意外错误降级为一条错误工具结果，
        交由模型自行纠偏。
        """
        call_id = f"call_{index}"
        name = ""
        arguments: dict[str, Any] = {}
        try:
            if not isinstance(call, dict):
                raise ApiError(422, "LLM_FORMAT_ERROR", "工具调用结构非法（应为对象）")
            call_id = str(call.get("id") or f"call_{index}")
            function = call.get("function")
            if not isinstance(function, dict):
                raise ApiError(422, "LLM_FORMAT_ERROR", "工具调用缺少 function 定义")
            name = str(function.get("name") or "")
            return await self._dispatch_validated(call_id, name, function.get("arguments"))
        except ApiError as exc:
            return self._error_result(call_id, name, arguments, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001 - 兜底：任何意外错误都不应中断编排
            logger.exception("工具调用出现未预期异常：%s", name or "(未知)")
            return self._error_result(call_id, name, arguments, "TOOL_EXECUTION_ERROR", f"工具执行异常：{exc}")

    async def _dispatch_validated(
        self, call_id: str, name: str, raw_arguments: Any
    ) -> tuple[ToolCallRecord, dict[str, Any], str | None]:
        """校验工具名与入参后执行。"""
        arguments: dict[str, Any] = {}
        if name not in _TOOL_NAMES:
            return self._error_result(call_id, name, arguments, "VALIDATION_ERROR", f"未知工具：{name or '(空)'}")
        arguments = _parse_arguments(raw_arguments)
        missing = [key for key in _REQUIRED[name] if arguments.get(key) in (None, "")]
        if missing:
            message = f"工具 {name} 缺少必填参数：{', '.join(missing)}"
            return self._error_result(call_id, name, arguments, "VALIDATION_ERROR", message)
        return await self._execute(call_id, name, arguments)

    @staticmethod
    def _error_result(
        call_id: str, name: str, arguments: dict[str, Any], code: str, message: str
    ) -> tuple[ToolCallRecord, dict[str, Any], str | None]:
        """构造一条错误工具结果（回填给模型，供其纠偏）。"""
        METRICS.inc_counter("agent_tool_calls_total", {"tool": name or "unknown", "ok": "false"})
        record = ToolCallRecord(name=name, arguments=arguments, ok=False, error=message)
        payload = json.dumps({"error": code, "message": message}, ensure_ascii=False)
        return record, {"role": "tool", "tool_call_id": call_id, "content": payload}, None

    async def _execute(
        self, call_id: str, name: str, arguments: dict[str, Any]
    ) -> tuple[ToolCallRecord, dict[str, Any], str | None]:
        """执行工具并把结果 / 错误回填给模型。"""
        result = await self._dispatcher.dispatch(name, arguments)
        METRICS.inc_counter("agent_tool_calls_total", {"tool": name, "ok": "true"})
        record = ToolCallRecord(name=name, arguments=arguments, ok=True, result=result)
        plan_id = result.get("planId") if isinstance(result, dict) else None
        payload = _dump(result)
        return record, {"role": "tool", "tool_call_id": call_id, "content": payload}, plan_id


def _dump(value: Any) -> str:
    """把工具结果序列化为回填文本并按上限截断。"""
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) > _MAX_TOOL_RESULT_CHARS:
        return text[:_MAX_TOOL_RESULT_CHARS] + "\n…（结果过长已截断）"
    return text


__all__ = ["AgentRunner"]
"""Agent function calling 编排循环。

流程：构造消息 → 调用 LLM → 若有 ``tool_calls`` 则并行调用内部算法引擎并回填结果 →
再次调用 LLM，直至模型给出最终文本或达到最大步数上限。

设计要点：
- 所有算法结果均来自工具（引擎）返回值，模型不得臆造；
- 单次工具失败不会中断整轮编排，而是把错误作为工具结果回填，让模型自行纠偏；
- 同一条 assistant 消息中的多个 ``tool_calls`` **并行执行**（``asyncio.gather``，保持原序回填）；
- 上下文预算超限时自动裁剪**最旧的用户回合**，保证当前请求与系统提示始终完整；
- 会话历史经 :class:`SessionStore` 持久化（默认内存 + TTL，可切 Redis），同会话串行化；
- **写操作两阶段**：写类工具（``plan_iteration`` / ``replan_all`` / ``task_add`` /
  ``task_update`` / ``task_delete``）不在编排循环内直接执行，而是登记为
  :class:`PendingAction` 返回前端；用户确认后经 :meth:`AgentRunner.confirm_actions`
  统一落库（只读工具仍在循环内即时执行，保证问答与排查可正常进行）。
- **检索增强（RAG + 长期记忆）**：每轮自动召回知识库片段与相关长期记忆并作为独立
  system 区块注入（增强而非必需，检索失败自动降级）；同时向模型暴露 ``kb_search`` /
  ``kb_list_documents`` / ``memory_search`` 只读工具，支持 agentic 检索；
  对话结束后**后台**提取本轮长期记忆（不阻塞响应）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, AsyncIterator

from ..constants import (
    SSE_DELTA,
    SSE_FINAL,
    SSE_META,
    SSE_PENDING,
    SSE_RESULT,
    SSE_TOOL_RESULT,
    SSE_TOOL_START,
)
from ..errors import ApiError, bad_request, llm_unavailable, not_found
from ..kb.pipeline import KnowledgeBaseService
from ..metrics import METRICS
from ..models import AgentChatResponse, AgentRunResult, KbHit, MemoryItem, PendingAction, ToolCallRecord
from ..sessions import InMemorySessionStore, SessionStore
from .llm import LlmClient
from .memory import MemoryService
from .prompts import ITERATION_PLANNER_SYSTEM_PROMPT, build_chat_instruction, build_plan_instruction
from .schemas import TOOL_SPECS, WRITE_TOOLS, describe_action
from .tools import ToolDispatcher

logger = logging.getLogger("iteration_planner.agent")

#: 回填给模型的单条工具结果上限（字符），避免超大计划撑爆上下文
_MAX_TOOL_RESULT_CHARS = 60_000

#: 工具名 → 必填参数（用于执行前的结构校验）
_REQUIRED: dict[str, list[str]] = {
    spec["function"]["name"]: list(spec["function"]["parameters"].get("required", [])) for spec in TOOL_SPECS
}
_TOOL_NAMES: frozenset[str] = frozenset(_REQUIRED)

#: 依赖「知识库 / 长期记忆」服务才可用的工具 → 所需服务名（服务缺失时不出现在工具清单中，
#: 避免模型调用一个必然失败的工具而浪费步数）
_SERVICE_BOUND_TOOLS: dict[str, str] = {
    "kb_search": "knowledge",
    "kb_list_documents": "knowledge",
    "memory_search": "memory",
}


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
        knowledge: KnowledgeBaseService | None = None,
        memory: MemoryService | None = None,
        kb_auto_inject: bool = True,
        kb_inject_top_k: int = 3,
        memory_top_k: int = 4,
    ) -> None:
        self._llm = llm
        self._dispatcher = dispatcher
        self._max_steps = max_steps
        self._temperature = temperature
        self._store: SessionStore = store or InMemorySessionStore()
        self._max_context_chars = max_context_chars
        self._knowledge = knowledge
        self._memory = memory
        self._kb_auto_inject = kb_auto_inject
        self._kb_inject_top_k = kb_inject_top_k
        self._memory_top_k = memory_top_k
        # 仅暴露「当前真正可用」的工具：检索类工具在对应服务缺失时会被移除
        available = {"knowledge": knowledge, "memory": memory}
        self._tool_specs = [
            spec
            for spec in TOOL_SPECS
            if available.get(_SERVICE_BOUND_TOOLS.get(spec["function"]["name"], ""), True) is not None
        ]

    @property
    def tool_specs(self) -> list[dict[str, Any]]:
        """当前实际可调用的工具定义（检索类工具在服务缺失时已被剔除）。"""
        return self._tool_specs

    @property
    def tool_names(self) -> list[str]:
        """当前实际可调用的工具名（供 ``/agent/health`` 与前端提示）。"""
        return [spec["function"]["name"] for spec in self._tool_specs]

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
        """自然语言需求 → 完整计划（端到端一次编排）。

        同时按需求文本召回知识库片段（若启用）注入上下文：历史迭代文档 / 规范 / 约束
        往往直接决定拆解粒度与依赖关系，比让模型凭空推断更可靠。
        """
        instruction = build_plan_instruction(
            requirement,
            iteration_deadline=iteration_deadline,
            iteration_start_date=iteration_start_date,
            dev_member_count=dev_member_count,
        )
        kb_context, references = await self._retrieve_knowledge(requirement)
        messages = self._base_messages(kb_context) + [{"role": "user", "content": instruction}]
        result = await self._loop(messages)
        result.references = [KbHit(**hit) for hit in references]
        return result

    async def run_chat(
        self,
        session_id: str,
        message: str,
        *,
        defer_writes: bool = True,
        plan_id: str | None = None,
    ) -> AgentRunResult:
        """会话式对话：携带历史上下文继续编排（同会话串行化）。

        ``defer_writes`` 为真时，本轮识别到的写操作不会立即执行，而是随结果返回
        待确认动作；同时**覆盖式**刷新该会话的待确认槽位（新一轮对话会作废上一轮
        未确认的动作，避免陈旧提案被误执行）。

        ``plan_id`` 为前端上报的「当前计划」，以可信上下文注入本轮指令。

        每轮还会按当前输入召回**长期记忆**与**知识库片段**并注入独立 system 区块，
        对话结束后在后台提取本轮记忆（不阻塞响应）。
        """
        async with self._store.lock(session_id):
            history = await self._store.get(session_id) or []
            instruction = build_chat_instruction(message, plan_id)
            memory_context, memories = await self._retrieve_memories(message)
            kb_context, references = await self._retrieve_knowledge(message)
            messages = (
                self._base_messages(memory_context, kb_context) + history + [{"role": "user", "content": instruction}]
            )
            result = await self._loop(messages, defer_writes=defer_writes)
            result.references = [KbHit(**hit) for hit in references]
            result.memories = [MemoryItem(**item) for item in memories]

            # 仅保留「用户 / 最终助手」两条，工具中间态不入历史，控制上下文体积
            # 历史存原始（已消毒）文本，边界标记仅用于本回合送入模型的即时输入
            updated = history + [
                {"role": "user", "content": message},
                {"role": "assistant", "content": result.content},
            ]
            await self._store.set(session_id, updated)
            if defer_writes:
                await self._store.set_pending(session_id, [item.model_dump() for item in result.pendingActions])
            # 后台提取长期记忆：LLM 调用有延迟，不占用本轮响应时间
            if self._memory is not None:
                self._memory.schedule_extract(session_id, message, result.content)
            return result

    async def run_chat_stream(
        self,
        session_id: str,
        message: str,
        *,
        defer_writes: bool = True,
        plan_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """流式版 :meth:`run_chat`：以 SSE 事件序列逐步产出（打字机式输出）。

        事件顺序：``meta``（会话 ID + 检索增强上下文）→ 若干 ``delta`` / ``tool_start`` /
        ``tool_result`` / ``pending`` → ``final``（完整结果）。事件名常量见
        ``app.constants.SSE_*``。

        与 :meth:`run_chat` **完全共享**同一套编排、会话持久化与后台记忆提取逻辑，
        仅把「一次性返回」改为「边编排边下发」，因此两条链路的业务语义严格一致。
        """
        async with self._store.lock(session_id):
            history = await self._store.get(session_id) or []
            instruction = build_chat_instruction(message, plan_id)
            memory_context, memories = await self._retrieve_memories(message)
            kb_context, references = await self._retrieve_knowledge(message)
            messages = (
                self._base_messages(memory_context, kb_context) + history + [{"role": "user", "content": instruction}]
            )
            # 首帧即携带检索增强上下文：前端可提前渲染「参考依据 / 召回记忆」卡片
            yield {"event": SSE_META, "data": {"sessionId": session_id, "references": references, "memories": memories}}

            result: AgentRunResult | None = None
            async for event in self._loop_stream(messages, defer_writes=defer_writes):
                if event["event"] == SSE_RESULT:
                    result = event["data"]
                    continue
                yield event
            if result is None:  # pragma: no cover - 编排必然产出结果或抛出异常
                raise ApiError(500, "INTERNAL_ERROR", "Agent 编排未产出结果")

            result.references = [KbHit(**hit) for hit in references]
            result.memories = [MemoryItem(**item) for item in memories]

            # 仅保留「用户 / 最终助手」两条，工具中间态不入历史，控制上下文体积
            updated = history + [
                {"role": "user", "content": message},
                {"role": "assistant", "content": result.content},
            ]
            await self._store.set(session_id, updated)
            if defer_writes:
                await self._store.set_pending(session_id, [item.model_dump() for item in result.pendingActions])
            if self._memory is not None:
                self._memory.schedule_extract(session_id, message, result.content)
            response = AgentChatResponse(sessionId=session_id, **result.model_dump())
            yield {"event": SSE_FINAL, "data": response.model_dump()}

    async def confirm_actions(
        self, session_id: str, action_ids: list[str] | None = None, *, reject: bool = False
    ) -> tuple[list[ToolCallRecord], list[PendingAction], str | None]:
        """执行（或驳回）待确认写操作（两阶段写操作的第二阶段）。

        :param action_ids: 要执行的 ``actionId``；``None`` 表示确认全部
        :param reject: 驳回模式——作废全部待确认动作、不执行任何写操作（会话上下文保留）
        :return: ``(执行记录, 仍待确认的动作, 产生的计划 ID)``

        逐个**串行**执行（每次写操作都会触发引擎全量重规划，串行可避免写冲突）；
        单个动作失败不影响其余动作；未确认的动作保留在会话中，可再次确认。
        """
        async with self._store.lock(session_id):
            stored = await self._store.get_pending(session_id) or []
            pending = [PendingAction.model_validate(item) for item in stored if isinstance(item, dict)]

            if reject:
                # 驳回是幂等操作：即使当前没有待确认动作也返回成功语义
                await self._store.set_pending(session_id, [])
                METRICS.inc_counter("agent_confirm_total", {"outcome": "rejected"})
                logger.info("用户驳回待确认写操作：会话 %s，作废 %d 项", session_id, len(pending))
                return [], [], None

            if not pending:
                raise not_found("没有待确认的操作（可能已执行、已取消或会话已过期），请重新发起请求")

            wanted = set(action_ids) if action_ids is not None else None
            if wanted is not None:
                unknown = sorted(wanted - {item.actionId for item in pending})
                if unknown:
                    raise bad_request(f"待确认动作不存在：{'、'.join(unknown[:5])}")
            selected = [item for item in pending if wanted is None or item.actionId in wanted]
            remaining = [item for item in pending if wanted is not None and item.actionId not in wanted]

            records: list[ToolCallRecord] = []
            plan_id: str | None = None
            for item in selected:
                record, produced = await self._execute_action(item)
                records.append(record)
                # 任务级写操作的结果信封里 planId 嵌套在 plan 下，回落到动作自身携带的 planId
                plan_id = produced or item.planId or plan_id
                if not record.ok:
                    logger.warning("待确认动作执行失败：%s · %s", item.tool, record.error)

            await self._store.set_pending(session_id, [item.model_dump() for item in remaining])
            await self._append_execution_history(session_id, selected, records)
            METRICS.inc_counter("agent_confirm_total", {"outcome": "executed" if records else "empty"})
            return records, remaining, plan_id

    async def reset_session(self, session_id: str) -> bool:
        """清空指定会话（含待确认动作），返回是否存在。"""
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
    def _base_messages(*blocks: str | None) -> list[dict[str, Any]]:
        """系统提示 + 检索增强区块（长期记忆 / 知识库片段）。

        区块作为**独立 system 消息**注入而非拼接进主提示：主提示保持稳定（利于缓存命中），
        检索结果随每轮变化，且顺序固定（记忆在前、知识库在后），便于排查上下文内容。
        """
        messages: list[dict[str, Any]] = [{"role": "system", "content": ITERATION_PLANNER_SYSTEM_PROMPT}]
        for block in blocks:
            if block:
                messages.append({"role": "system", "content": block})
        return messages

    async def _retrieve_knowledge(self, query: str) -> tuple[str | None, list[dict[str, Any]]]:
        """召回知识库上下文与命中片段（未启用 / 失败时返回空）。"""
        if self._knowledge is None or not self._kb_auto_inject:
            return None, []
        return await self._knowledge.context_and_hits(query, top_k=self._kb_inject_top_k)

    async def _retrieve_memories(self, query: str) -> tuple[str | None, list[dict[str, Any]]]:
        """召回长期记忆上下文与记忆列表（未启用 / 失败时返回空）。"""
        if self._memory is None:
            return None, []
        return await self._memory.context_and_memories(query, top_k=self._memory_top_k)

    async def _loop(self, messages: list[dict[str, Any]], *, defer_writes: bool = False) -> AgentRunResult:
        """非流式编排：消费 :meth:`_loop_stream` 并取回终态结果。

        保持「单一实现」：流式与非流式共用同一套循环，避免两条链路语义漂移。
        """
        async for event in self._loop_stream(messages, defer_writes=defer_writes):
            if event["event"] == SSE_RESULT:
                return event["data"]
        raise ApiError(500, "INTERNAL_ERROR", "Agent 编排未产出结果")  # pragma: no cover

    async def _loop_stream(
        self, messages: list[dict[str, Any]], *, defer_writes: bool = False
    ) -> AsyncIterator[dict[str, Any]]:
        """编排循环的**流式实现**：边调用模型/工具边产出事件。

        产出 ``delta`` / ``tool_start`` / ``tool_result`` / ``pending`` 事件（可直接下发给
        前端），并以 ``SSE_RESULT`` 事件收尾（内部使用，携带 :class:`AgentRunResult`，
        由调用方转换为 ``final`` 或直接返回）。

        模型未实现 ``chat_stream`` 时自动回退为一次性调用：仅退化为「无逐字效果」，
        功能与语义不变。
        """
        if self._llm is None:
            raise llm_unavailable()

        streamer = getattr(self._llm, "chat_stream", None)
        records: list[ToolCallRecord] = []
        pending: list[PendingAction] = []
        plan_id: str | None = None

        for step in range(1, self._max_steps + 1):
            self._enforce_budget(messages)
            message: dict[str, Any] | None = None
            if streamer is None:
                # 模型未实现流式能力：一次性调用，并把全文作为单帧下发（功能不变，仅少了逐字效果）
                message = await self._llm.chat(messages, tools=self._tool_specs, temperature=self._temperature)
                text = _content_to_text(message.get("content"))
                if text:
                    yield {"event": SSE_DELTA, "data": {"text": text}}
            else:
                async for chunk in streamer(messages, tools=self._tool_specs, temperature=self._temperature):
                    if chunk.get("type") == "content":
                        yield {"event": SSE_DELTA, "data": {"text": chunk["text"]}}
                    elif chunk.get("type") == "message":
                        message = chunk.get("message")
                if message is None:
                    raise ApiError(422, "LLM_FORMAT_ERROR", "LLM 流式响应未返回完整消息")

            messages.append(self._canonical_assistant(message))
            tool_calls = message.get("tool_calls") or []

            if not tool_calls:
                METRICS.observe("agent_steps", step, buckets=(1, 2, 3, 4, 5, 6, 8), help="Agent 收敛步数")
                METRICS.inc_counter("agent_runs_total", {"outcome": "completed"})
                yield {
                    "event": SSE_RESULT,
                    "data": AgentRunResult(
                        content=_content_to_text(message.get("content")),
                        planId=plan_id,
                        toolCalls=records,
                        steps=step,
                    ),
                }
                return

            for call in tool_calls:
                function = call.get("function") if isinstance(call, dict) else None
                name = function.get("name") if isinstance(function, dict) else None
                yield {"event": SSE_TOOL_START, "data": {"name": name}}

            # 同一条消息内的多个工具调用并行执行，结果按原序回填（OpenAI 协议要求顺序一致）
            outcomes = await asyncio.gather(
                *(self._run_tool_call(index, call, defer_writes) for index, call in enumerate(tool_calls))
            )
            for record, tool_message, produced_plan_id, pending_action in outcomes:
                records.append(record)
                plan_id = produced_plan_id or plan_id
                if tool_message is not None:
                    messages.append(tool_message)
                if pending_action is not None:
                    pending.append(pending_action)
                # result 可能很大（整份计划），此处只下发轻量字段，完整明细由 final 事件补齐
                yield {
                    "event": SSE_TOOL_RESULT,
                    "data": {
                        "name": record.name,
                        "arguments": record.arguments,
                        "ok": record.ok,
                        "error": record.error,
                    },
                }

            if pending:
                # 出现写操作：本轮到此为止，交由用户确认后再落库（不继续消耗模型步数）
                pending = _dedupe_pending(pending)
                METRICS.observe("agent_steps", step, buckets=(1, 2, 3, 4, 5, 6, 8), help="Agent 收敛步数")
                METRICS.inc_counter("agent_runs_total", {"outcome": "pending_confirmation"})
                narrative = _content_to_text(message.get("content")).strip()
                summary = _describe_pending(pending)
                # 待确认摘要由服务端确定性生成：以增量下发，保证前端文案与最终结果一致
                yield {"event": SSE_DELTA, "data": {"text": f"{narrative}\n\n{summary}" if narrative else summary}}
                yield {"event": SSE_PENDING, "data": {"actions": [item.model_dump() for item in pending]}}
                yield {
                    "event": SSE_RESULT,
                    "data": AgentRunResult(
                        content=f"{narrative}\n\n{summary}" if narrative else summary,
                        planId=plan_id,
                        toolCalls=records,
                        steps=step,
                        pendingActions=pending,
                    ),
                }
                return

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
        self, index: int, call: Any, defer_writes: bool = False
    ) -> tuple[ToolCallRecord, dict[str, Any] | None, str | None, PendingAction | None]:
        """校验 + 执行单个工具调用（任何异常都以错误结果回填，绝不中断整轮编排）。

        并行执行（``asyncio.gather``）下，单个子任务抛出非预期异常会导致整批失败并中断
        Agent 循环，因此这里做**兜底捕获**：把任何意外错误降级为一条错误工具结果，
        交由模型自行纠偏。

        返回 ``(记录, 回填消息, 产生的 planId, 待确认动作)``：当 ``defer_writes`` 为真且
        命中写类工具时，不执行而是返回待确认动作（回填消息为 ``None``）。
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
            return await self._dispatch_validated(call_id, name, function.get("arguments"), defer_writes)
        except ApiError as exc:
            record, tool_message = self._error_result(call_id, name, arguments, exc.code, exc.message)
            return record, tool_message, None, None
        except Exception as exc:  # noqa: BLE001 - 兜底：任何意外错误都不应中断编排
            logger.exception("工具调用出现未预期异常：%s", name or "(未知)")
            record, tool_message = self._error_result(
                call_id, name, arguments, "TOOL_EXECUTION_ERROR", f"工具执行异常：{exc}"
            )
            return record, tool_message, None, None

    async def _dispatch_validated(
        self, call_id: str, name: str, raw_arguments: Any, defer_writes: bool
    ) -> tuple[ToolCallRecord, dict[str, Any] | None, str | None, PendingAction | None]:
        """校验工具名与入参后执行（或登记为待确认写操作）。"""
        arguments: dict[str, Any] = {}
        if name not in _TOOL_NAMES:
            record, tool_message = self._error_result(
                call_id, name, arguments, "VALIDATION_ERROR", f"未知工具：{name or '(空)'}"
            )
            return record, tool_message, None, None
        arguments = _parse_arguments(raw_arguments)
        missing = [key for key in _REQUIRED[name] if arguments.get(key) in (None, "")]
        if missing:
            message = f"工具 {name} 缺少必填参数：{', '.join(missing)}"
            record, tool_message = self._error_result(call_id, name, arguments, "VALIDATION_ERROR", message)
            return record, tool_message, None, None
        if defer_writes and name in WRITE_TOOLS:
            return self._register_pending(name, arguments)
        record, tool_message, plan_id = await self._execute(call_id, name, arguments)
        return record, tool_message, plan_id, None

    @staticmethod
    def _register_pending(
        name: str, arguments: dict[str, Any]
    ) -> tuple[ToolCallRecord, None, None, PendingAction]:
        """把写类工具登记为待确认动作（不触碰引擎）。"""
        plan_ref = arguments.get("planId")
        action = PendingAction(
            actionId=f"act_{uuid.uuid4().hex[:12]}",
            tool=name,
            label=describe_action(name, arguments),
            arguments=arguments,
            planId=plan_ref if isinstance(plan_ref, str) and plan_ref else None,
        )
        METRICS.inc_counter("agent_tool_calls_total", {"tool": name, "ok": "deferred"})
        record = ToolCallRecord(
            name=name,
            arguments=arguments,
            ok=True,
            result={"status": "PENDING_CONFIRMATION", "actionId": action.actionId},
        )
        return record, None, None, action

    @staticmethod
    def _error_result(
        call_id: str, name: str, arguments: dict[str, Any], code: str, message: str
    ) -> tuple[ToolCallRecord, dict[str, Any]]:
        """构造一条错误工具结果（回填给模型，供其纠偏）。"""
        METRICS.inc_counter("agent_tool_calls_total", {"tool": name or "unknown", "ok": "false"})
        record = ToolCallRecord(name=name, arguments=arguments, ok=False, error=message)
        payload = json.dumps({"error": code, "message": message}, ensure_ascii=False)
        return record, {"role": "tool", "tool_call_id": call_id, "content": payload}

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

    async def _execute_action(self, action: PendingAction) -> tuple[ToolCallRecord, str | None]:
        """执行单个已确认的写操作（失败降级为错误记录，不抛出）。"""
        try:
            result = await self._dispatcher.dispatch(action.tool, action.arguments)
        except ApiError as exc:
            METRICS.inc_counter("agent_tool_calls_total", {"tool": action.tool, "ok": "false"})
            return ToolCallRecord(name=action.tool, arguments=action.arguments, ok=False, error=exc.message), None
        except Exception as exc:  # noqa: BLE001 - 兜底：单个动作失败不应影响其余动作
            logger.exception("待确认动作执行异常：%s", action.tool)
            METRICS.inc_counter("agent_tool_calls_total", {"tool": action.tool, "ok": "false"})
            return (
                ToolCallRecord(name=action.tool, arguments=action.arguments, ok=False, error=f"工具执行异常：{exc}"),
                None,
            )
        METRICS.inc_counter("agent_tool_calls_total", {"tool": action.tool, "ok": "true"})
        plan_id = result.get("planId") if isinstance(result, dict) else None
        return ToolCallRecord(name=action.tool, arguments=action.arguments, ok=True, result=result), plan_id

    async def _append_execution_history(
        self, session_id: str, selected: list[PendingAction], records: list[ToolCallRecord]
    ) -> None:
        """把确认执行的结果写入会话历史（供后续追问引用，历史只存可读摘要）。"""
        if not records:
            return
        lines: list[str] = []
        for item, record in zip(selected, records):
            outcome = "已执行" if record.ok else f"失败（{record.error or '未知原因'}）"
            lines.append(f"- {item.label}：{outcome}")
        summary = "用户已确认并执行以下写操作：\n" + "\n".join(lines)
        history = await self._store.get(session_id) or []
        await self._store.set(session_id, history + [{"role": "assistant", "content": summary}])


def _dedupe_pending(pending: list[PendingAction]) -> list[PendingAction]:
    """同一轮内模型可能重复提交同一写操作，按「工具 + 入参」去重（保留首次出现）。"""
    seen: set[str] = set()
    unique: list[PendingAction] = []
    for item in pending:
        key = f"{item.tool}|{json.dumps(item.arguments, ensure_ascii=False, sort_keys=True, default=str)}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _describe_pending(pending: list[PendingAction]) -> str:
    """确定性生成「待确认」摘要文本（不依赖模型输出，保证降级场景下也可读）。"""
    lines = [f"{index}. {item.label}" for index, item in enumerate(pending, start=1)]
    return (
        f"本次拟执行以下 {len(pending)} 项写操作，**需您确认后才会真正落库**：\n"
        + "\n".join(lines)
        + "\n\n点击确认后我将立即执行，并由算法引擎完成全量重规划。"
    )


def _dump(value: Any) -> str:
    """把工具结果序列化为回填文本并按上限截断。"""
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) > _MAX_TOOL_RESULT_CHARS:
        return text[:_MAX_TOOL_RESULT_CHARS] + "\n…（结果过长已截断）"
    return text


__all__ = ["AgentRunner"]
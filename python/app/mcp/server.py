"""MCP 协议实现（JSON-RPC 2.0 over MCP 2024-11-05）。

**为什么复用 Agent 的 ``TOOL_SPECS``**：MCP 与 Agent 面对的是同一批底层能力（同一批引擎
REST 接口）。若为 MCP 另写一份工具清单，立刻就会出现「Agent 能调 15 个工具、MCP 只暴露 12 个」
以及「参数名在两处不一致」这类口径漂移，最终由使用方承担。因此工具定义**单一来源**：
``app.agent.schemas.TOOL_SPECS`` 直接被翻译为 MCP 的 ``tools/list`` 结果。

**写操作的两阶段语义在 MCP 侧同样成立**（与 Web Agent 一致，不能因为换了个协议就放宽）：
写类工具（``plan_iteration`` / ``replan_all`` / ``task_add`` / ``task_update`` / ``task_delete``）
不落库，只登记为该 MCP 会话的**待确认动作**；客户端需再调用 ``confirm_pending_actions``
才真正写入。这两个 MCP 专属工具（``list_pending_actions`` / ``confirm_pending_actions``）
在 Web Agent 上没有对应物，是因为 Web 端有确认卡片 UI，而 MCP 只有工具调用。

鉴权：MCP 端点使用**自己的 Bearer 令牌**（``MCP_TOKEN``，缺省回落 ``API_TOKEN``），
比对采用常量时间函数，避免逐字符比较泄露令牌前缀。
"""

from __future__ import annotations

import hmac
import json
import logging
import uuid
from typing import Any

from .. import __version__
from ..agent.sanitize import unwrap_arguments
from ..agent.schemas import (
    TOOL_ACTION_LABELS,
    TOOL_SPECS,
    WRITE_TOOLS,
    describe_action,
)
from ..constants import (
    MCP_ERR_INTERNAL,
    MCP_ERR_INVALID_PARAMS,
    MCP_ERR_INVALID_REQUEST,
    MCP_ERR_METHOD_NOT_FOUND,
    MCP_JSONRPC,
    MCP_METHOD_INITIALIZE,
    MCP_METHOD_INITIALIZED,
    MCP_METHOD_PING,
    MCP_METHOD_TOOLS_CALL,
    MCP_METHOD_TOOLS_LIST,
    MCP_PROTOCOL_VERSION,
    MCP_SESSION_PREFIX,
    MCP_TOOL_CONFIRM_PENDING,
    MCP_TOOL_LIST_PENDING,
)
from ..errors import ApiError
from ..metrics import METRICS
from ..models import PendingAction, ToolCallRecord

logger = logging.getLogger("iteration_planner.mcp")

#: 工具名 → 必填参数（与 Agent 侧同一算法：直接由 schema 派生，避免手写清单漂移）
_REQUIRED: dict[str, list[str]] = {
    spec["function"]["name"]: list(spec["function"]["parameters"].get("required", [])) for spec in TOOL_SPECS
}

#: MCP 专属工具（Web Agent 用确认卡片承担的角色，在 MCP 侧必须由工具承担）
EXTRA_TOOLS: list[dict[str, Any]] = [
    {
        "name": MCP_TOOL_LIST_PENDING,
        "description": "列出本 MCP 会话中已登记但**尚未落库**的写操作（两阶段写确认的第一阶段产物）。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": MCP_TOOL_CONFIRM_PENDING,
        "description": (
            "确认执行（或驳回）已登记的写操作——**调用本工具才会真正落库**。"
            "缺省确认全部；传 actionIds 可只确认部分；reject=true 表示驳回且不执行。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "actionIds": {"type": "array", "items": {"type": "string"}, "description": "只确认这些 actionId；缺省全部"},
                "reject": {"type": "boolean", "description": "驳回模式：作废待确认动作，不执行写入"},
            },
            "additionalProperties": False,
        },
    },
]

_EXTRA_TOOL_NAMES: frozenset[str] = frozenset(spec["name"] for spec in EXTRA_TOOLS)


def _constant_time_equals(left: str, right: str) -> bool:
    """常量时间字符串比较（长度不同也走一次 digest，避免长度侧信道）。"""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def verify_token(provided: str, *, mcp_token: str, api_token: str, auth_enabled: bool) -> bool:
    """校验 MCP Bearer 令牌。

    接受 ``MCP_TOKEN`` 与 ``API_TOKEN`` 两者之一（前者专用、后者复用内部服务账号令牌）。
    两者都未配置时：仅当全局鉴权关闭（本地开发）才放行，否则**拒绝**——否则把 MCP 暴露成
    一个无凭据即可建计划、改成员的后门，而这正是自动化集成最容易被漏审的入口。
    """
    accepted = [token for token in (mcp_token, api_token) if token]
    if not accepted:
        return not auth_enabled
    if not provided:
        return False
    return any(_constant_time_equals(provided, token) for token in accepted)


def build_mcp_server(
    *,
    dispatcher: Any,
    store: Any,
    settings: Any,
    available_tools: frozenset[str] | None = None,
) -> McpServer:
    """按配置装配 MCP 服务端（工具清单按当前真正可用的能力收敛）。"""
    names = available_tools
    if names is None:
        names = frozenset(spec["function"]["name"] for spec in TOOL_SPECS)
    return McpServer(
        dispatcher=dispatcher,
        store=store,
        server_name=settings.mcp_server_name,
        max_result_chars=settings.mcp_max_result_chars,
        available_tools=frozenset(names) | _EXTRA_TOOL_NAMES,
    )


class McpServer:
    """无状态（除会话待确认动作外）的 MCP 服务端。

    MCP 会话只承载一件事：**待确认的写操作**。会话 ID 复用 :class:`SessionStore`
    （内存 / Redis 可插拔）作为存储，因此多副本部署下「在 A 副本登记、到 B 副本确认」
    同样成立 —— 这正是把 MCP 会话挂在既有存储上、而不是在进程内自建字典的原因。
    """

    def __init__(
        self,
        *,
        dispatcher: Any,
        store: Any,
        server_name: str = "iteration-planner",
        max_result_chars: int = 200_000,
        available_tools: frozenset[str] | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._store = store
        self._server_name = server_name
        self._max_result_chars = max(1024, int(max_result_chars))
        # 两个 MCP 专属工具属于**传输层**（换协议补齐两阶段写确认），不属于 Agent 能力：
        # 无论调用方如何裁剪业务工具，都必须始终可用；否则写工具会被登记却永远无法确认。
        self._available = (available_tools or frozenset(_REQUIRED)) | frozenset(_EXTRA_TOOL_NAMES)

    # ------------------------------- 工具清单 -------------------------------

    @property
    def tool_names(self) -> list[str]:
        return [spec["name"] for spec in self.tool_specs]

    @property
    def tool_specs(self) -> list[dict[str, Any]]:
        """MCP ``tools/list`` 结果：由 OpenAI function calling schema 翻译而来。"""
        specs: list[dict[str, Any]] = []
        for spec in TOOL_SPECS:
            function = spec["function"]
            name = function["name"]
            if name not in self._available:
                continue
            specs.append(
                {
                    "name": name,
                    "description": function["description"],
                    "inputSchema": function["parameters"],
                }
            )
        specs.extend(spec for spec in EXTRA_TOOLS if spec["name"] in self._available)
        return specs

    # ------------------------------- 协议入口 -------------------------------

    async def handle(
        self, message: Any, *, session_id: str | None = None
    ) -> tuple[Any, str | None]:
        """处理一条 JSON-RPC 消息（或一批）。

        :return: ``(响应或 None, 需要回写给客户端的 MCP 会话 ID)``。
            通知（``id`` 缺省）不产生响应，故返回 ``None``。
        """
        if isinstance(message, list):
            responses: list[dict[str, Any]] = []
            session = session_id
            for item in message:
                response, session = await self.handle(item, session_id=session)
                if response is not None:
                    responses.append(response)
            if not responses:
                return None, session
            return responses, session
        if not isinstance(message, dict):
            return self._error(None, MCP_ERR_INVALID_REQUEST, "JSON-RPC 消息必须是对象"), session_id

        msg_id = message.get("id")
        method = message.get("method")
        if not isinstance(method, str):
            if msg_id is None:
                return None, session_id
            return self._error(msg_id, MCP_ERR_INVALID_REQUEST, "缺少 method 字段"), session_id

        if method == MCP_METHOD_INITIALIZE:
            session = session_id or self._new_session_id()
            result = {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self._server_name, "version": __version__},
                "instructions": (
                    "本服务把「研发迭代规划」能力暴露为工具：读类工具即时执行（读计划 / 关键路径 / "
                    "风险 / 报表 / 知识库 / 记忆）；写类工具（建计划 / 重规划 / 增删改任务）**只登记待确认**，"
                    "必须再调用 confirm_pending_actions 才真正落库。"
                ),
            }
            return self._ok(msg_id, result), session

        if method == MCP_METHOD_INITIALIZED:
            return None, session_id  # 客户端通知，无响应

        if method == MCP_METHOD_PING:
            return self._ok(msg_id, {}), session_id

        if method == MCP_METHOD_TOOLS_LIST:
            return self._ok(msg_id, {"tools": self.tool_specs}), session_id

        if method == MCP_METHOD_TOOLS_CALL:
            return await self._handle_tools_call(message, session_id)

        if msg_id is None:
            return None, session_id  # 未知通知：按规范静默忽略
        return self._error(msg_id, MCP_ERR_METHOD_NOT_FOUND, f"未知方法：{method}"), session_id

    async def _handle_tools_call(self, message: dict[str, Any], session_id: str | None) -> tuple[Any, str | None]:
        msg_id = message.get("id")
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        name = params.get("name")
        arguments = params.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}
        if not isinstance(name, str) or not name:
            return self._error(msg_id, MCP_ERR_INVALID_PARAMS, "缺少工具名"), session_id
        if name not in self._available:
            return self._error(msg_id, MCP_ERR_INVALID_PARAMS, f"未知工具：{name}"), session_id

        session = session_id or self._new_session_id()
        METRICS.inc_counter("mcp_tool_calls_total", {"tool": name})
        try:
            data, is_error = await self._dispatch_tool(name, arguments, session)
        except ApiError as exc:
            # 业务错误按 MCP 规范作为**工具结果**返回（isError=true），而不是 JSON-RPC 错误：
            # 客户端（模型）需要看到失败原因才能自纠，JSON-RPC 错误会让整轮对话直接中断。
            METRICS.inc_counter("mcp_tool_calls_total", {"tool": name, "ok": "false"})
            return self._ok(msg_id, self._tool_result({"error": exc.code, "message": exc.message}, is_error=True)), session
        except Exception:  # noqa: BLE001 - 兜底：任何意外都不得把堆栈回给客户端
            logger.exception("MCP 工具执行异常：%s", name)
            METRICS.inc_counter("mcp_tool_calls_total", {"tool": name, "ok": "false"})
            return self._ok(msg_id, self._tool_result({"error": "INTERNAL_ERROR", "message": "工具执行异常"}, is_error=True)), session
        METRICS.inc_counter("mcp_tool_calls_total", {"tool": name, "ok": "true"})
        return self._ok(msg_id, self._tool_result(data, is_error=is_error)), session

    # ------------------------------- 工具执行 -------------------------------

    async def _dispatch_tool(self, name: str, arguments: dict[str, Any], session: str) -> tuple[Any, bool]:
        """执行工具；返回 ``(数据, 是否为错误结果)``。"""
        if name not in _EXTRA_TOOL_NAMES:
            # 入参来自外部 Agent 的自动生成：与 Web Agent 同一口径，先还原不可信边界脚手架，
            # 避免「送入模型时加的边界说明」被当作业务数据落库并扩散到新计划。
            arguments = unwrap_arguments(arguments)
            missing = [key for key in _REQUIRED.get(name, []) if arguments.get(key) in (None, "")]
            if missing:
                raise ApiError(400, "VALIDATION_ERROR", f"工具 {name} 缺少必填参数：{', '.join(missing)}")
            if name in WRITE_TOOLS:
                return await self._register_pending(name, arguments, session), False
            return await self._dispatcher.dispatch(name, arguments), False

        if name == MCP_TOOL_LIST_PENDING:
            return {"pendingActions": await self._read_pending(session)}, False
        return await self._confirm_pending(arguments, session), False

    async def _read_pending(self, session: str) -> list[dict[str, Any]]:
        stored = await self._store.get_pending(self._session_key(session)) or []
        return [item for item in stored if isinstance(item, dict)]

    async def _register_pending(self, name: str, arguments: dict[str, Any], session: str) -> dict[str, Any]:
        """把写类工具登记为该 MCP 会话的待确认动作（不落库）。"""
        action = PendingAction(
            actionId=f"act_{uuid.uuid4().hex[:12]}",
            tool=name,
            label=describe_action(name, arguments),
            arguments=arguments,
            planId=arguments.get("planId") if isinstance(arguments.get("planId"), str) else None,
        )
        key = self._session_key(session)
        # 会话锁：同一 MCP 会话的并发登记不得互相覆盖（内存实现为 asyncio.Lock，
        # Redis 实现为租约锁，跨副本成立）
        async with self._store.lock(key):
            pending = await self._read_pending(session)
            pending.append(action.model_dump())
            await self._store.set_pending(key, pending)
        return {
            "status": "PENDING_CONFIRMATION",
            "actionId": action.actionId,
            "label": action.label,
            "action": TOOL_ACTION_LABELS.get(name, name),
            "pendingCount": len(pending),
            "hint": f"该操作尚未落库。请调用 {MCP_TOOL_CONFIRM_PENDING} 确认后才会真正执行。",
        }

    async def _confirm_pending(self, arguments: dict[str, Any], session: str) -> dict[str, Any]:
        """确认执行 / 驳回待确认动作（**此为真正的落库动作**）。"""
        reject = bool(arguments.get("reject"))
        action_ids = arguments.get("actionIds")
        wanted = {str(item) for item in action_ids} if isinstance(action_ids, list) else None
        key = self._session_key(session)
        async with self._store.lock(key):
            pending = [PendingAction.model_validate(item) for item in await self._read_pending(session)]
            if reject:
                await self._store.set_pending(key, [])
                METRICS.inc_counter("mcp_confirm_total", {"outcome": "rejected"})
                return {"rejected": True, "executed": [], "remaining": [], "message": "已驳回，未执行任何写操作"}
            if not pending:
                raise ApiError(404, "NOT_FOUND", "没有待确认的操作（可能已执行、已驳回或会话已过期）")
            if wanted is not None:
                unknown = sorted(wanted - {item.actionId for item in pending})
                if unknown:
                    raise ApiError(400, "VALIDATION_ERROR", f"待确认动作不存在：{'、'.join(unknown[:5])}")
            selected = [item for item in pending if wanted is None or item.actionId in wanted]
            remaining = [item for item in pending if wanted is not None and item.actionId not in wanted]

            records: list[ToolCallRecord] = []
            plan_id: str | None = None
            for item in selected:
                # 串行执行：每次写都会触发引擎全量重规划，并发会互相打架
                try:
                    result = await self._dispatcher.dispatch(item.tool, item.arguments)
                except ApiError as exc:
                    records.append(
                        ToolCallRecord(name=item.tool, arguments=item.arguments, ok=False, error=exc.message, code=exc.code)
                    )
                    continue
                plan_id = (result.get("planId") if isinstance(result, dict) else None) or item.planId or plan_id
                records.append(ToolCallRecord(name=item.tool, arguments=item.arguments, ok=True, result=result))
            await self._store.set_pending(key, [item.model_dump() for item in remaining])
        METRICS.inc_counter("mcp_confirm_total", {"outcome": "executed"})
        return {
            "executed": [record.model_dump() for record in records],
            "remaining": [item.model_dump() for item in remaining],
            "planId": plan_id,
            "message": "已执行 " + str(sum(1 for record in records if record.ok)) + f" / {len(records)} 项写操作",
        }

    # ------------------------------- 会话生命周期 -------------------------------

    async def terminate_session(self, session: str) -> None:
        """终止 MCP 会话：丢弃其待确认动作（**绝不**顺带执行它们）。

        显式丢弃而不是留着过期：调用方主动 DELETE 的语义就是「我不要这批改动了」，
        留着等 TTL 反而会让后续确认意外落库一批已被放弃的写操作。
        """
        key = self._session_key(session)
        async with self._store.lock(key):
            await self._store.set_pending(key, [])
        METRICS.inc_counter("mcp_requests_total", {"outcome": "session_terminated"})

    # ------------------------------- 内部实现 -------------------------------

    def _session_key(self, session: str) -> str:
        return f"{MCP_SESSION_PREFIX}{session}"

    def _new_session_id(self) -> str:
        # 会话 ID 是持有即代表身份的凭据（凭它可确认该会话登记的写操作），因此取
        # uuid4 的**完整 128 bit**，不做截断（截断只会削弱凭据强度，换不来任何收益）
        return uuid.uuid4().hex

    def _tool_result(self, data: Any, *, is_error: bool) -> dict[str, Any]:
        text = json.dumps(data, ensure_ascii=False, default=str)
        if len(text) > self._max_result_chars:
            text = text[: self._max_result_chars] + "\n…（结果过长已截断，请用更精确的参数缩小范围）"
            METRICS.inc_counter("mcp_result_truncated_total")
        result: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": is_error}
        if isinstance(data, dict):
            result["structuredContent"] = data
        return result

    def _ok(self, msg_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": MCP_JSONRPC, "id": msg_id, "result": result}

    def _error(self, msg_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": MCP_JSONRPC, "id": msg_id, "error": {"code": code, "message": message}}


__all__ = [
    "McpServer",
    "build_mcp_server",
    "verify_token",
    "EXTRA_TOOLS",
    "MCP_ERR_INTERNAL",
]

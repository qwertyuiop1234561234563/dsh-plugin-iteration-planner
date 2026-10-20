"""MCP Streamable HTTP 传输（``POST /api/mcp``）。

MCP 规范定义的 Streamable HTTP 传输：**单一端点**，客户端把 JSON-RPC 消息 POST 上来，
服务端既可以一次性返回 JSON（本实现），也可以返回 SSE 流。本实现选择「一次性 JSON」：

- 本服务的所有工具都是**有限耗时的引擎 / 向量检索调用**（秒级），没有需要流式推送的
  长任务；为此额外维护 SSE 与断线重连只会增加一条难以测试的路径。
- 进度类长任务（如整份计划重规划）本来就有 :mod:`app.routes.jobs` 的作业 + 进度订阅，
  由 ``tools/call`` 返回 jobId 更贴合 MCP「工具返回结果」的语义。

因此 ``GET`` 明确回 **405**（规范允许「不提供 SSE 流」的服务这样响应），并给出 ``Allow``
首部说明可用方法；``DELETE`` 用于**终止会话**（清空该会话的待确认动作）。

鉴权在本路由内完成（该端点已登记为免鉴权端点，见 :data:`app.auth.permissions.PUBLIC_ENDPOINTS`）：
按 MCP 规范使用 ``Authorization: Bearer`` 携带 ``MCP_TOKEN``（缺省回落 ``API_TOKEN``），
比对为**常量时间**。请求体在**读取过程中**增量限额（不是读完再判），避免无界 JSON-RPC
载荷先把内存打爆再返回 413。
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from ..constants import (
    MCP_ERR_PARSE,
    MCP_JSONRPC,
    MCP_SESSION_HEADER,
)
from ..errors import ApiError, not_found, unauthorized
from ..mcp.server import verify_token
from ..metrics import METRICS
from ..state import AppContext

logger = logging.getLogger("iteration_planner.mcp.http")


def _ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[len("Bearer ") :] if header.startswith("Bearer ") else ""


def _jsonrpc_error(msg_id: object, code: int, message: str) -> dict[str, object]:
    return {"jsonrpc": MCP_JSONRPC, "id": msg_id, "error": {"code": code, "message": message}}


async def _read_body(request: Request, limit: int) -> bytes:
    """增量读取请求体并在超限时**立刻中断**（读完再判会让内存先被占满）。

    这里是 MCP 端点自己的上限（``MCP_MAX_BODY_BYTES``）：它比业务请求体上限小得多，
    因为 MCP 载荷是结构化的工具调用，不该出现几十 MB 的正文。
    """
    total = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise ApiError(413, "PAYLOAD_TOO_LARGE", f"请求体超过上限 {limit} 字节")
        chunks.append(chunk)
    return b"".join(chunks)


def build_mcp_router(base_path: str) -> APIRouter:
    """构建 MCP 传输路由（挂在 ``{base_path}/mcp``）。"""
    router = APIRouter(prefix=f"{base_path}/mcp", tags=["mcp"])

    @router.post("")
    async def mcp_post(request: Request) -> Response:
        ctx = _ctx(request)
        server = ctx.mcp
        if server is None:
            raise not_found("MCP 服务未启用（MCP_ENABLED=false）")
        settings = ctx.settings
        if not verify_token(
            _bearer(request),
            mcp_token=settings.mcp_token,
            api_token=settings.api_token,
            auth_enabled=settings.auth_enabled,
        ):
            METRICS.inc_counter("mcp_requests_total", {"outcome": "unauthorized"})
            response = JSONResponse(status_code=401, content=unauthorized("MCP 令牌无效").to_body())
            # 按 MCP / HTTP 惯例给出挑战首部，便于客户端判断该用哪种凭据
            response.headers["WWW-Authenticate"] = 'Bearer realm="mcp"'
            return response

        try:
            raw = await _read_body(request, settings.mcp_max_body_bytes)
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except ApiError as exc:
            METRICS.inc_counter("mcp_requests_total", {"outcome": "too_large"})
            return JSONResponse(status_code=exc.status_code, content=exc.to_body())
        except (UnicodeDecodeError, json.JSONDecodeError):
            # 解析失败：按 JSON-RPC 规范回 -32700（此时 id 未知，置 null）
            METRICS.inc_counter("mcp_requests_total", {"outcome": "parse_error"})
            return JSONResponse(status_code=400, content=_jsonrpc_error(None, MCP_ERR_PARSE, "JSON 解析失败"))
        if payload is None:
            return JSONResponse(status_code=400, content=_jsonrpc_error(None, MCP_ERR_PARSE, "空的 JSON-RPC 载荷"))

        session_id = request.headers.get(MCP_SESSION_HEADER) or None
        response_body, new_session = await server.handle(payload, session_id=session_id)
        METRICS.inc_counter("mcp_requests_total", {"outcome": "ok"})
        if response_body is None:
            # 只包含通知：规范要求回 202 且无响应体
            return Response(status_code=202)
        result = JSONResponse(content=response_body)
        if new_session and new_session != session_id:
            # 服务端分配的会话 ID 通过首部下发（规范约定），客户端后续请求需原样带回
            result.headers[MCP_SESSION_HEADER] = new_session
        return result

    @router.delete("")
    async def mcp_delete(request: Request) -> Response:
        """终止 MCP 会话：清空该会话登记的待确认动作（**不执行**任何写操作）。"""
        ctx = _ctx(request)
        server = ctx.mcp
        if server is None:
            raise not_found("MCP 服务未启用（MCP_ENABLED=false）")
        settings = ctx.settings
        if not verify_token(
            _bearer(request),
            mcp_token=settings.mcp_token,
            api_token=settings.api_token,
            auth_enabled=settings.auth_enabled,
        ):
            response = JSONResponse(status_code=401, content=unauthorized("MCP 令牌无效").to_body())
            response.headers["WWW-Authenticate"] = 'Bearer realm="mcp"'
            return response
        session_id = request.headers.get(MCP_SESSION_HEADER) or ""
        if session_id:
            await server.terminate_session(session_id)
        return Response(status_code=204)

    @router.get("")
    async def mcp_get() -> Response:
        """本服务不提供服务端→客户端的 SSE 流（规范允许），明确回 405 并说明可用方法。"""
        response = JSONResponse(
            status_code=405,
            content={
                "code": "METHOD_NOT_ALLOWED",
                "message": "本 MCP 端点不提供 SSE 下行流；请用 POST 发送 JSON-RPC，用 DELETE 终止会话。",
            },
        )
        response.headers["Allow"] = "POST, DELETE"
        return response

    return router


__all__ = ["build_mcp_router"]

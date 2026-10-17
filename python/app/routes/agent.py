"""Agent 原生端点（``/api/agent/*``）。

这些端点是 Python 侧独有的编排能力：以自然语言需求驱动 Agent 调用内部算法引擎，
完成端到端规划或会话式修改。所有响应遵循与引擎一致的信封 ``{data: ...}``。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from typing import Any, AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ..agent.advisor import generate_advice
from ..agent.sanitize import sanitize_plan_id, sanitize_session_id, sanitize_user_input
from ..agent.schemas import WRITE_TOOL_NAMES
from ..constants import SSE_ERROR, SSE_HEARTBEAT_SECONDS
from ..errors import ApiError, bad_request, internal, llm_unavailable, not_found
from ..models import (
    AdviceRequest,
    AgentChatRequest,
    AgentChatResponse,
    AgentConfirmRequest,
    AgentConfirmResponse,
    AgentPlanRequest,
    AgentRunResult,
)
from ..state import AppContext

logger = logging.getLogger("iteration_planner.agent.stream")


def _sse_event(name: str, data: Any) -> str:
    """把事件编码为 SSE 帧。

    ``data`` 统一 JSON 序列化为**单行**（JSON 会转义换行），保证不破坏 SSE 帧边界。
    """
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {name}\ndata: {payload}\n\n"


def _require_agent(ctx: AppContext):
    if not ctx.runner.available:
        raise llm_unavailable()
    return ctx.runner


def _sanitized_input(raw: str, *, field: str) -> str:
    """消毒用户输入；只含空白 / 不可见字符时**明确拒绝**。

    Pydantic 的 ``min_length=1`` 只约束原始字符串，``"   "`` 或纯控制字符都能通过校验，
    但消毒后为空：若照原样送进编排，会在会话历史中留下空 ``content`` 的用户消息，
    多数 OpenAI 兼容服务端会直接拒绝后续请求（该会话从此不可用）。因此这里统一拦截。
    """
    cleaned = sanitize_user_input(raw)
    if not cleaned:
        raise bad_request(f"{field}为空或仅包含空白 / 不可见字符，请重新输入")
    return cleaned


def _new_session_id() -> str:
    """新建会话 ID。

    会话 ID 是**持有即代表身份**的凭据（凭它可读回对话历史、确认该会话登记中的写操作），
    因此取 ``uuid4``（CPython 用 ``os.urandom`` 生成，密码学随机）的**完整 128 bit**，
    不做长度截断——截断只会削弱凭据强度，换不来任何收益。
    """
    return uuid.uuid4().hex


def build_agent_router(base: str) -> APIRouter:
    router = APIRouter(prefix=f"{base}/agent", tags=["agent"])

    @router.get("/health", summary="Agent 可用性（LLM / 知识库 / 长期记忆）")
    async def agent_health(request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        return {
            "data": {
                "enabled": ctx.runner.available,
                "llm": ctx.settings.llm_model or None,
                # 工具清单按「当前真正可用」返回：未启用向量化时不含 kb_* / memory_* 工具
                "tools": ctx.runner.tool_names,
                # 写类工具清单：前端据此向用户明示「哪些操作需要确认」
                "writeTools": list(WRITE_TOOL_NAMES),
                # 检索增强能力状态（前端据此决定是否展示知识库 / 记忆卡片）
                "knowledgeEnabled": ctx.knowledge is not None,
                "memoryEnabled": ctx.memory is not None,
                "memoryAutoExtract": bool(ctx.memory and ctx.memory.auto_extract_enabled),
                "embeddingModel": ctx.settings.embedding_model if ctx.settings.embedding_enabled else None,
            }
        }

    @router.get("/tools", summary="Agent 可调用的工具清单（OpenAI function calling 格式）")
    async def agent_tools(request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        return {"data": ctx.runner.tool_specs}

    @router.post("/plan", summary="自然语言需求 → 完整迭代计划（端到端编排）")
    async def agent_plan(payload: AgentPlanRequest, request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        runner = _require_agent(ctx)
        result = await runner.run_plan(
            _sanitized_input(payload.requirement, field="需求原文"),
            iteration_deadline=payload.iterationDeadline,
            iteration_start_date=payload.iterationStartDate,
            dev_member_count=payload.devMemberCount,
        )
        return {"data": result.model_dump()}

    @router.post("/chat", summary="会话式对话（携带上下文，可多轮修改计划）")
    async def agent_chat(payload: AgentChatRequest, request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        runner = _require_agent(ctx)
        session_id = payload.sessionId or _new_session_id()
        result: AgentRunResult = await runner.run_chat(
            session_id,
            _sanitized_input(payload.message, field="消息内容"),
            defer_writes=payload.deferWrites,
            plan_id=sanitize_plan_id(payload.planId),
        )
        response = AgentChatResponse(sessionId=session_id, **result.model_dump())
        return {"data": response.model_dump()}

    @router.post("/chat/stream", summary="会话式对话（SSE 流式输出，逐字下发）")
    async def agent_chat_stream(payload: AgentChatRequest, request: Request) -> StreamingResponse:
        """与 ``/agent/chat`` 语义完全一致，仅把「一次性响应」改为 **SSE 逐事件下发**。

        事件序列：``meta`` → 若干 ``delta`` / ``tool_start`` / ``tool_result`` / ``pending``
        → ``final``（完整结果）；出错时以 ``error`` 事件收尾（HTTP 状态已是 200，故错误
        必须以事件形式送达，前端据此降级）。空闲超过心跳间隔发送注释行，避免代理/网关
        因空闲断开长连接。
        """
        ctx: AppContext = request.app.state.ctx
        runner = _require_agent(ctx)
        session_id = payload.sessionId or _new_session_id()
        message = _sanitized_input(payload.message, field="消息内容")
        plan_id = sanitize_plan_id(payload.planId)

        async def event_source() -> AsyncIterator[str]:
            # 把编排生成器包成 Task：心跳超时只发送注释行，绝不取消进行中的一步编排
            # （取消会中断 LLM / 引擎调用并提前释放会话锁，属于错误行为）。
            pipeline = runner.run_chat_stream(
                session_id, message, defer_writes=payload.deferWrites, plan_id=plan_id
            )
            pending_step: asyncio.Task[Any] | None = None
            try:
                while True:
                    if pending_step is None:
                        pending_step = asyncio.ensure_future(pipeline.__anext__())
                    done, _ = await asyncio.wait({pending_step}, timeout=SSE_HEARTBEAT_SECONDS)
                    if not done:
                        yield ": ping\n\n"
                        continue
                    finished, pending_step = pending_step, None
                    try:
                        event = finished.result()
                    except StopAsyncIteration:
                        return
                    except ApiError as exc:
                        yield _sse_event(SSE_ERROR, exc.to_body())
                        return
                    except Exception:  # noqa: BLE001 - 流已建立，异常必须以事件送达
                        # 同上：只下发稳定错误码 + 通用文案，异常细节仅留在服务端日志
                        logger.exception("Agent 流式编排异常：会话 %s", session_id)
                        yield _sse_event(SSE_ERROR, internal().to_body())
                        return
                    yield _sse_event(event["event"], event["data"])
            finally:
                # 客户端断开 / 服务端取消：终止上游编排并释放会话锁
                if pending_step is not None and not pending_step.done():
                    pending_step.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await pending_step
                with contextlib.suppress(Exception):
                    await pipeline.aclose()

        return StreamingResponse(
            event_source(),
            media_type="text/event-stream",
            headers={
                # no-transform：禁止中间层压缩/改写（压缩会缓冲而破坏逐字效果）
                "Cache-Control": "no-cache, no-transform",
                # 显式告知 nginx 关闭缓冲（否则 SSE 会被攒够一整块才下发）
                "X-Accel-Buffering": "no",
            },
        )

    @router.post("/confirm", summary="确认执行待确认的写操作（两阶段写操作的第二步）")
    async def agent_confirm(payload: AgentConfirmRequest, request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        # 确认执行只依赖引擎（写操作本身），不依赖 LLM：即使模型网关不可用，已登记的
        # 动作仍应允许用户确认落库（例如模型网关中途故障）。
        executed, remaining, plan_id = await ctx.runner.confirm_actions(
            payload.sessionId, payload.actionIds, reject=payload.reject
        )
        response = AgentConfirmResponse(
            sessionId=payload.sessionId,
            planId=plan_id,
            executed=executed,
            remainingActions=remaining,
            rejected=payload.reject,
        )
        return {"data": response.model_dump()}

    @router.post("/advise", summary="基于引擎风险清单生成缓解建议（LLM 优先，缺失则规则模式）")
    async def agent_advise(payload: AdviceRequest, request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        result = await generate_advice(
            ctx.engine,
            ctx.runner,
            model=ctx.settings.llm_model or None,
            plan_id=payload.planId,
        )
        return {"data": result.model_dump()}

    @router.delete("/sessions/{session_id}", summary="清空指定会话上下文")
    async def reset_session(session_id: str, request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        # 会话 ID 是「持有即代表身份」的凭据，且会作为会话存储键使用：路径参数不受
        # pydantic 约束，此处按同一字符集白名单校验；非法与不存在同样回报 NOT_FOUND，
        # 避免把「格式探测」与「是否存在」区分开来。
        resolved = sanitize_session_id(session_id)
        if resolved is None or not await ctx.runner.reset_session(resolved):
            # 不回显原始路径参数：非法 ID 会走到这里，原样拼回消息等于反射用户输入
            raise not_found("会话不存在或已被清空")
        return {"data": {"sessionId": resolved, "cleared": True}}

    return router


__all__ = ["build_agent_router"]
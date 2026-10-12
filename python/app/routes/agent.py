"""Agent 原生端点（``/api/agent/*``）。

这些端点是 Python 侧独有的编排能力：以自然语言需求驱动 Agent 调用内部算法引擎，
完成端到端规划或会话式修改。所有响应遵循与引擎一致的信封 ``{data: ...}``。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request

from ..agent.advisor import generate_advice
from ..agent.sanitize import sanitize_user_input
from ..agent.schemas import TOOL_NAMES, TOOL_SPECS
from ..errors import llm_unavailable, not_found
from ..models import AdviceRequest, AgentChatRequest, AgentChatResponse, AgentPlanRequest, AgentRunResult
from ..state import AppContext


def _require_agent(ctx: AppContext):
    if not ctx.runner.available:
        raise llm_unavailable()
    return ctx.runner


def build_agent_router(base: str) -> APIRouter:
    router = APIRouter(prefix=f"{base}/agent", tags=["agent"])

    @router.get("/health", summary="Agent 可用性（是否已配置 LLM 网关）")
    async def agent_health(request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        return {
            "data": {
                "enabled": ctx.runner.available,
                "llm": ctx.settings.llm_model or None,
                "tools": TOOL_NAMES,
            }
        }

    @router.get("/tools", summary="Agent 可调用的工具清单（OpenAI function calling 格式）")
    async def agent_tools() -> dict:
        return {"data": TOOL_SPECS}

    @router.post("/plan", summary="自然语言需求 → 完整迭代计划（端到端编排）")
    async def agent_plan(payload: AgentPlanRequest, request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        runner = _require_agent(ctx)
        result = await runner.run_plan(
            sanitize_user_input(payload.requirement),
            iteration_deadline=payload.iterationDeadline,
            iteration_start_date=payload.iterationStartDate,
            dev_member_count=payload.devMemberCount,
        )
        return {"data": result.model_dump()}

    @router.post("/chat", summary="会话式对话（携带上下文，可多轮修改计划）")
    async def agent_chat(payload: AgentChatRequest, request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        runner = _require_agent(ctx)
        session_id = payload.sessionId or uuid.uuid4().hex[:16]
        result: AgentRunResult = await runner.run_chat(session_id, sanitize_user_input(payload.message))
        response = AgentChatResponse(sessionId=session_id, **result.model_dump())
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
        if not await ctx.runner.reset_session(session_id):
            raise not_found(f"会话不存在：{session_id}")
        return {"data": {"sessionId": session_id, "cleared": True}}

    return router


__all__ = ["build_agent_router"]
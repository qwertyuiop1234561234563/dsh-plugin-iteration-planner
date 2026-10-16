"""Agent 原生端点（``/api/agent/*``）。

这些端点是 Python 侧独有的编排能力：以自然语言需求驱动 Agent 调用内部算法引擎，
完成端到端规划或会话式修改。所有响应遵循与引擎一致的信封 ``{data: ...}``。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request

from ..agent.advisor import generate_advice
from ..agent.sanitize import sanitize_plan_id, sanitize_user_input
from ..agent.schemas import WRITE_TOOL_NAMES
from ..errors import llm_unavailable, not_found
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


def _require_agent(ctx: AppContext):
    if not ctx.runner.available:
        raise llm_unavailable()
    return ctx.runner


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
        result: AgentRunResult = await runner.run_chat(
            session_id,
            sanitize_user_input(payload.message),
            defer_writes=payload.deferWrites,
            plan_id=sanitize_plan_id(payload.planId),
        )
        response = AgentChatResponse(sessionId=session_id, **result.model_dump())
        return {"data": response.model_dump()}

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
        if not await ctx.runner.reset_session(session_id):
            raise not_found(f"会话不存在：{session_id}")
        return {"data": {"sessionId": session_id, "cleared": True}}

    return router


__all__ = ["build_agent_router"]
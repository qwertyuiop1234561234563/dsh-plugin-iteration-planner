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
from ..agent.collab import run_collaboration
from ..agent.retro import run_retro
from ..agent.sanitize import sanitize_plan_id, sanitize_session_id, sanitize_user_input
from ..agent.schemas import WRITE_TOOL_NAMES
from ..agent.whatif import run_whatif
from ..auth import current_principal
from ..constants import PERM_MEMORY_WRITE, SSE_ERROR, SSE_HEARTBEAT_SECONDS
from ..errors import ApiError, bad_request, internal, llm_unavailable, not_found
from ..models import (
    AdviceRequest,
    AgentChatRequest,
    AgentChatResponse,
    AgentCollabRequest,
    AgentCollabStep,
    AgentConfirmRequest,
    AgentConfirmResponse,
    AgentPlanRequest,
    AgentRunResult,
    PatrolFindingsResponse,
    PatrolRunResponse,
    RetroRequest,
    WhatIfRequest,
)
from ..state import AppContext

logger = logging.getLogger("iteration_planner.agent.stream")


def _sse_event(name: str, data: Any) -> str:
    """把事件编码为 SSE 帧。

    ``data`` 统一 JSON 序列化为**单行**（JSON 会转义换行），保证不破坏 SSE 帧边界。
    """
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {name}\ndata: {payload}\n\n"


def _ensure_plan_scope(request: Request, plan_id: str) -> None:
    """校验调用方对请求体中 ``planId`` 的可见范围（路径式检查覆盖不到 body 参数）。

    受限主体（访客等仅可见被指派计划）访问未授权计划时一律 **404** 而非 403：
    不向调用方泄露「该计划存在但不归你」这一信息（与路径式检查口径一致）。
    """
    principal = current_principal(request)
    if not principal.can_see_plan(plan_id):
        raise not_found("计划不存在")


def _require_agent(ctx: AppContext):
    if not ctx.runner.available:
        raise llm_unavailable()
    return ctx.runner


def _owner_of(request: Request) -> str:
    """当前主体标识：作为检查点台账的归属键，决定「谁能看到 / 续跑这条台账」。

    不传 owner 会让台账落到 ``anonymous``，用户之后在自己的列表里根本看不到它——
    断点续跑就退化成了「只有重启前那一个进程能看见」。
    """
    principal = getattr(request.state, "principal", None)
    return principal.user_id if principal is not None else ""


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


def _stream_response(
    pipeline: AsyncIterator[dict[str, Any]],
    *,
    log_label: str,
) -> StreamingResponse:
    """把「事件生成器」包装为带心跳的 SSE 响应（会话流与协作流共用同一实现）。

    心跳只发注释行，**绝不取消进行中的一步编排**（取消会中断 LLM / 引擎调用并提前释放
    会话锁，属于错误行为）；客户端断开时在 ``finally`` 里终止上游生成器。
    """

    async def event_source() -> AsyncIterator[str]:
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
                    # 只下发稳定错误码 + 通用文案，异常细节仅留在服务端日志
                    logger.exception("%s 流式编排异常", log_label)
                    yield _sse_event(SSE_ERROR, internal().to_body())
                    return
                yield _sse_event(event["event"], event["data"])
        finally:
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
            owner=_owner_of(request),
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
            owner=_owner_of(request),
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

        # 把编排生成器包成 Task：心跳超时只发送注释行，绝不取消进行中的一步编排
        # （取消会中断 LLM / 引擎调用并提前释放会话锁，属于错误行为）。
        pipeline = runner.run_chat_stream(
            session_id, message, defer_writes=payload.deferWrites, plan_id=plan_id, owner=_owner_of(request)
        )
        return _stream_response(pipeline, log_label=f"会话 {session_id}")

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

    @router.post("/whatif", summary="What-if 沙盘：自然语言情景 → 引擎重算 → before/after 增量对比")
    async def agent_whatif(payload: WhatIfRequest, request: Request) -> dict:
        """「后端延期 3 天」「再加 2 个人」这类假设，直接问「会影响什么」而**不污染真实计划**。

        实现要点：解析（LLM 优先 / 规则降级）→ 引擎建**临时**计划并应用任务级扰动 →
        汇总关键路径增删、交付日期偏移、新增 / 解除风险 → **回收临时计划**。
        所有数值来自引擎，Python 不做任何排期推导。
        """
        ctx: AppContext = request.app.state.ctx
        plan_id = sanitize_plan_id(payload.planId)
        if plan_id is None:
            raise bad_request("缺少或非法的 planId")
        _ensure_plan_scope(request, plan_id)
        result = await run_whatif(
            ctx.engine,
            ctx.runner,
            plan_id=plan_id,
            instruction=_sanitized_input(payload.instruction, field="情景描述"),
            model=ctx.settings.llm_model or None,
            max_task_edits=ctx.settings.whatif_max_task_edits,
        )
        return {"data": result.model_dump()}

    @router.post("/collab/stream", summary="多角色协作编排（SSE：需求分析→引擎排期→估算审阅→风险缓解→质量校验）")
    async def agent_collab_stream(payload: AgentCollabRequest, request: Request) -> StreamingResponse:
        """一句话需求 → 多角色协作产出完整计划，**每步可见、可中断**。

        事件序列：若干 ``step``（角色时间线）+ ``tool_start`` / ``tool_result``（引擎排期计算）
        + ``pending``（待确认的估算修正）+ ``final``（结论与计划）。角色不可用时该步标为
        ``skipped`` 并说明原因，其余步骤继续；引擎（排期角色）失败则整轮终止。
        """
        ctx: AppContext = request.app.state.ctx
        requirement = _sanitized_input(payload.requirement, field="需求原文")
        pipeline = run_collaboration(
            ctx.engine,
            ctx.runner,
            ctx.dispatcher,
            requirement=requirement,
            iteration_deadline=payload.iterationDeadline,
            iteration_start_date=payload.iterationStartDate,
            dev_member_count=payload.devMemberCount,
            max_steps=ctx.settings.agent_collab_max_steps,
        )
        return _stream_response(pipeline, log_label="多角色协作")

    @router.get("/collab/steps", summary="多角色协作的角色与步骤说明（前端时间线展示用）")
    async def agent_collab_steps() -> dict:
        from ..agent.collab import ROLE_LABELS

        return {
            "data": [
                AgentCollabStep(role=role, title=title, status="pending").model_dump() for role, title in ROLE_LABELS.items()
            ]
        }

    @router.post("/retro", summary="迭代复盘 + 学习：实际 vs 预估偏差 / 风险兑现率 / 估算校准（可写回记忆）")
    async def agent_retro(payload: RetroRequest, request: Request) -> dict:
        """复盘结论 = **确定性统计**（可复算）+ 模型研判（不可用时降级规则结论）。

        实际工时与「已兑现风险」是外部事实，引擎计划里没有对应字段，因此必须由调用方提供；
        未提供时统计会明确标注 ``available=false`` 并说明原因，**不会编造数字**。
        """
        ctx: AppContext = request.app.state.ctx
        plan_id = sanitize_plan_id(payload.planId)
        if plan_id is None:
            raise bad_request("缺少或非法的 planId")
        _ensure_plan_scope(request, plan_id)
        # 记忆回写是「写长期记忆」动作：需要 memory:write 权限（权限判定统一走权限矩阵，
        # 不在此处写角色名判断）
        principal = current_principal(request)
        may_write_memory = principal.has(PERM_MEMORY_WRITE)
        result = await run_retro(
            ctx.engine,
            ctx.runner,
            plan_id=plan_id,
            actual_hours=payload.actualHours,
            materialized_risk_ids=payload.materializedRiskIds,
            scope_changes=payload.scopeChanges,
            write_memory=payload.writeMemory and ctx.settings.retro_write_memory and may_write_memory,
            memory=ctx.memory,
            max_memories=ctx.settings.retro_max_memories,
            model=ctx.settings.llm_model or None,
        )
        return {"data": result.model_dump()}

    @router.get("/patrol/findings", summary="主动巡检发现列表（后台定时巡检的增量预警）")
    async def agent_patrol_findings(request: Request, limit: int = 50, planId: str | None = None) -> dict:
        ctx: AppContext = request.app.state.ctx
        if ctx.patrol is None:
            return {
                "data": PatrolFindingsResponse(
                    enabled=False, running=False, intervalSeconds=0.0, signals=[]
                ).model_dump()
            }
        scope = None
        if planId:
            resolved = sanitize_plan_id(planId)
            if resolved is None:
                raise bad_request("非法的 planId")
            scope = frozenset({resolved})
        items = ctx.patrol.findings(scope, limit=max(1, min(limit, 200)))
        status = ctx.patrol.status()
        response = PatrolFindingsResponse(
            items=items,
            total=len(items),
            running=bool(status.get("running")),
            enabled=True,
            intervalSeconds=ctx.settings.patrol_interval_seconds,
            lastRunAt=status.get("lastRunAt"),
            lastSummary=status.get("lastSummary") or {},
            signals=list(status.get("signals") or []),
        )
        return {"data": response.model_dump()}

    @router.post("/patrol/run", summary="立即执行一轮主动巡检（不改数据，只推送增量预警）")
    async def agent_patrol_run(request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        if ctx.patrol is None:
            raise bad_request("主动巡检未启用（PATROL_ENABLED=false）")
        summary = await ctx.patrol.run_once()
        return {"data": PatrolRunResponse(**summary).model_dump()}

    @router.get("/patrol/signals", summary="巡检信号与严重度说明（前端图例）")
    async def agent_patrol_signals() -> dict:
        from ..constants import PATROL_SEVERITIES, PATROL_SIGNAL_LABELS

        return {
            "data": {
                "signals": [{"name": name, "label": label} for name, label in PATROL_SIGNAL_LABELS.items()],
                "severities": list(PATROL_SEVERITIES),
            }
        }

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
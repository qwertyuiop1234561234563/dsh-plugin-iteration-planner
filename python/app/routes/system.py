"""系统路由：存活 / 就绪探针、指标暴露与 WebSocket 推送端点。

鉴权口径：探针与 ``/metrics`` 免鉴权（供编排器与 Prometheus 抓取）；``/ws``
**必须鉴权**——它推送的是「需求原文 + 任务明细」级业务载荷，且 WebSocket 握手走的是
独立 scope，不受 HTTP 鉴权中间件约束（见 :func:`ws_endpoint`）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse

from ..auth import Principal, build_principal, machine_principal
from ..constants import PLAN_SCOPE_ASSIGNED
from ..metrics import METRICS
from ..security import constant_time_equals, websocket_token
from ..state import AppContext

logger = logging.getLogger("iteration_planner.system")


async def _engine_health(ctx: AppContext) -> tuple[bool, dict]:
    """探测引擎健康；返回（是否可达, 引擎健康体）。"""
    try:
        response = await ctx.engine.forward("GET", f"{ctx.settings.api_base_path}/health")
    except Exception:  # noqa: BLE001 - 引擎不可达时降级，绝不抛错
        return False, {}
    if response.status_code >= 400:
        return False, {}
    try:
        return True, response.json()
    except ValueError:
        return True, {}


async def _collect_health(ctx: AppContext) -> dict:
    """健康检查：转发引擎状态 + 附带 Python 运行信息。"""
    reachable, engine = await _engine_health(ctx)
    return {
        "status": "ok" if reachable else "degraded",
        "version": engine.get("version", ctx.version),
        "storage": engine.get("storage", "unknown"),
        "uptimeMs": engine.get("uptimeMs", 0),
        "wsSubscribers": ctx.broadcaster.size,
        # Python 对外服务附加信息（前端类型为结构化忽略多余字段）
        "python": {
            "version": ctx.version,
            "uptimeMs": ctx.uptime_ms,
            "engineReachable": reachable,
            "engineCircuit": ctx.engine.breaker_state,
            "agentEnabled": ctx.runner.available,
            "agentTools": len(ctx.runner.tool_names),
            "knowledgeEnabled": ctx.knowledge is not None,
            "memoryEnabled": ctx.memory is not None,
            # 缓存是纯优化项：透出后端与命中率，便于判断「加速是否真的生效」
            "cache": ctx.cache.describe() if ctx.cache is not None else None,
            "rateLimitBackend": ctx.rate_limit.name if ctx.rate_limit is not None else None,
            # 作业存储与幂等后端：多副本部署时两者都必须落在 Redis，否则「异步作业」与
            # 「幂等键」只在单个副本内可见。透出后端名，便于运维一眼确认集群配置是否一致。
            "jobStore": ctx.jobs.describe() if ctx.jobs is not None else None,
            "idempotencyStore": ctx.idempotency.describe() if ctx.idempotency is not None else None,
            # 访问控制状态：便于运维一眼判断「是否需要登录」「登录是否已启用」
            "authEnabled": ctx.settings.auth_enabled,
            "authStore": "sqlite" if getattr(ctx, "auth", None) is not None else None,
        },
    }


def _ws_scope(websocket: WebSocket, ctx: AppContext) -> frozenset[str] | None:
    """解析该 WebSocket 连接的**可见计划集合**；``None`` 表示不受限。

    ``/ws`` 推送的是「需求原文 + 任务明细」级业务载荷，因此访客（guest）必须按计划白名单
    过滤，而不是把未授权计划的事件一并推给他。
    """
    principal = getattr(websocket.state, "principal", None)
    if principal is None:
        return None
    if principal.plan_scope == PLAN_SCOPE_ASSIGNED:
        return frozenset(principal.plan_ids)
    return None


async def _resolve_ws_principal(websocket: WebSocket, ctx: AppContext) -> Principal | None:
    """WebSocket 握手鉴权：``?token=``（机器令牌）→ 会话 Cookie（人类用户）。

    返回 ``None`` 表示拒绝握手。``AUTH_ENABLED=false`` 时返回机器主体（不鉴权）。
    """
    settings = ctx.settings
    if not settings.auth_enabled:
        return machine_principal()

    expected = settings.api_token
    if expected and constant_time_equals(websocket_token(websocket), expected):
        return machine_principal()

    store = getattr(ctx, "auth", None)
    if store is None:
        return None
    token = websocket.cookies.get(settings.auth_session_cookie, "")
    user = await store.load_session(token)
    if user is None:
        return None
    return build_principal(
        user_id=user["userId"],
        username=user["username"],
        display_name=user["displayName"],
        role=user["role"],
        plan_ids=tuple(await store.plan_ids(user["userId"])),
    )


def register_system_routes(app: FastAPI) -> None:
    """按配置注册 ``/healthz``、``/readyz``、``/metrics``、``/api/health`` 与 ``/ws``。"""
    settings = app.state.ctx.settings
    base = settings.api_base_path

    @app.get("/healthz", tags=["system"], summary="存活探针（容器 liveness，免鉴权，不依赖引擎）")
    async def healthz(request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        return {
            "status": "ok",
            "version": ctx.version,
            "uptimeMs": ctx.uptime_ms,
            "agentEnabled": ctx.runner.available,
            "wsClients": ctx.broadcaster.size,
            "engineBaseUrl": settings.engine_base_url,
        }

    @app.get("/readyz", tags=["system"], summary="就绪探针（readiness：引擎可达且未在关机）")
    async def readyz(request: Request) -> PlainTextResponse:
        ctx: AppContext = request.app.state.ctx
        if ctx.shutting_down:
            return PlainTextResponse(
                json.dumps({"status": "shutting_down", "ready": False}, ensure_ascii=False),
                status_code=503,
                media_type="application/json",
            )
        reachable, _ = await _engine_health(ctx)
        ready = reachable
        body = {
            "status": "ready" if ready else "not_ready",
            "ready": ready,
            "engineReachable": reachable,
            "engineCircuit": ctx.engine.breaker_state,
        }
        # LLM 为可选能力，不作为就绪门槛，仅透出状态供排障
        body["agentEnabled"] = ctx.runner.available
        return PlainTextResponse(
            json.dumps(body, ensure_ascii=False),
            status_code=200 if ready else 503,
            media_type="application/json",
        )

    @app.get(f"{base}/health", tags=["system"], summary="健康检查（转发引擎 + Python 运行信息，免鉴权）")
    async def api_health(request: Request) -> dict:
        return await _collect_health(request.app.state.ctx)

    @app.get("/metrics", tags=["system"], summary="Prometheus 指标（文本格式，免鉴权）")
    async def metrics() -> PlainTextResponse:
        return PlainTextResponse(METRICS.render(), media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.websocket(settings.api_ws_path)
    async def ws_endpoint(websocket: WebSocket) -> None:
        ctx: AppContext = websocket.app.state.ctx
        # WebSocket 握手的 scope 类型是 ``websocket``，**不经** ``@app.middleware("http")``
        # 那套鉴权（BaseHTTPMiddleware 只包装 http scope）。若不在此显式校验，任何未授权
        # 客户端都能直连 ``ws://<host>/ws`` 订阅到 plan.created 等事件的**全量载荷**
        # （含 rawRequirement 需求原文与 taskList 任务明细）。
        # 未通过校验时在 accept 之前关闭：握手阶段即被拒（HTTP 403），一条事件都不会下发。
        principal = await _resolve_ws_principal(websocket, ctx)
        if principal is None:
            METRICS.inc_counter(
                "ws_handshake_total",
                {"outcome": "unauthorized"},
                help="WebSocket 握手计数（按结果分桶：authorized / unauthorized）",
            )
            logger.warning("WebSocket 握手被拒：会话缺失或令牌不匹配（client=%s）", websocket.client)
            await websocket.close(code=1008)
            return
        websocket.state.principal = principal
        METRICS.inc_counter(
            "ws_handshake_total",
            {"outcome": "authorized"},
            help="WebSocket 握手计数（按结果分桶：authorized / unauthorized）",
        )
        broadcaster = ctx.broadcaster
        plan_ids = _ws_scope(websocket, ctx)
        # 连接数达上限时 connect() 已按 1013 关闭连接，此处不得再下发任何消息
        if not await broadcaster.connect(websocket, plan_ids=plan_ids):
            return
        try:
            # 握手确认帧（仅含时间戳，不含任何业务数据），对受作用域限制的访客连接同样下发，
            # 让前端有一条确定的「已建立」信号，而不必靠首个业务事件判断连接是否成功。
            await broadcaster.send(
                websocket, {"type": "connected", "at": datetime.now(timezone.utc).isoformat()}
            )
            # 断线重连补偿：补发缓冲中的最近事件，避免客户端漏掉断线期间的变更
            for payload in broadcaster.replay_payloads(plan_ids=plan_ids):
                await broadcaster.send(websocket, payload)
            while True:
                # 仅用于保活与感知断开；前端上行消息不参与业务
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            broadcaster.disconnect(websocket)


__all__ = ["register_system_routes"]
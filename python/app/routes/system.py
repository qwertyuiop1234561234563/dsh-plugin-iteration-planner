"""系统路由：存活 / 就绪探针、指标暴露与 WebSocket 推送端点（均免鉴权）。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse

from ..agent.schemas import TOOL_NAMES
from ..metrics import METRICS
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
            "agentTools": len(TOOL_NAMES),
        },
    }


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
        broadcaster = ctx.broadcaster
        await broadcaster.connect(websocket)
        try:
            await broadcaster.send(websocket, {"type": "connected", "at": datetime.now(timezone.utc).isoformat()})
            # 断线重连补偿：补发缓冲中的最近事件，避免客户端漏掉断线期间的变更
            for payload in broadcaster.replay_payloads():
                await broadcaster.send(websocket, payload)
            while True:
                # 仅用于保活与感知断开；前端上行消息不参与业务
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            broadcaster.disconnect(websocket)


__all__ = ["register_system_routes"]
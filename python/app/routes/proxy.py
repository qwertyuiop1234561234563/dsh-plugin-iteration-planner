"""对外网关：把 ``/api/**`` 透传给内部算法引擎，并托管前端 SPA 静态资源。

选择「透传」而非「重写」的原因：保证 Vue 控制台面对 Python 服务时零改动 ——
响应信封（``{data}`` / ``{code,message,details}``）、状态码、导出文件全部原样镜像。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from ..constants import (
    IDEMPOTENCY_KEY_HEADER,
    JOB_KIND_PLAN_CREATE,
    JOB_KIND_PLAN_REPLAN,
    PREFER_HEADER,
    PREFER_RESPOND_ASYNC,
    PLAN_SCOPE_ASSIGNED,
)
from ..errors import ApiError
from ..idempotency import is_idempotent_method, run_idempotent, validate_key
from ..jobs import submit_job

logger = logging.getLogger("iteration_planner.gateway")

#: 可异步化的写路径（相对 API 前缀）：这些接口在引擎侧是**同步阻塞**的算法计算
#: （实测 80s+），是「网关先超时、引擎后落库」这类孤儿写的高发点。
_ASYNC_JOB_PATHS = {
    "/plans": JOB_KIND_PLAN_CREATE,
}
_REPLAN_PATH = re.compile(r"^/plans/[^/]+/replan$")

#: 逐跳首部：不应由代理层转发（content-length / content-encoding 由响应体重新计算，
#: content-type 通过 media_type 单独设置以防重复）
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-encoding",
    "content-length",
    "content-type",
}


def _forward_headers(request: Request) -> dict[str, str]:
    """挑出需要透传给引擎的请求首部。"""
    headers: dict[str, str] = {}
    for key in ("content-type", "accept", "accept-language"):
        value = request.headers.get(key)
        if value:
            headers[key] = value
    return headers


def _response_headers(response) -> dict[str, str]:
    return {k: v for k, v in response.headers.items() if k.lower() not in _HOP_BY_HOP}


def register_proxy_and_static(app: FastAPI) -> None:
    """注册兜底路由：``/api/**`` 转发，其余 GET 交给 SPA。"""
    ctx = app.state.ctx
    base = ctx.settings.api_base_path
    static_dir = ctx.settings.web_static_dir

    @app.api_route(
        "/{full_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
        include_in_schema=False,
    )
    async def gateway(full_path: str, request: Request) -> Response:
        path = "/" + full_path

        if path.startswith(base):
            return await _forward(ctx, request, path)
        return _serve_static(static_dir, full_path, request)


def _plan_scope_filter(request: Request, path: str, base: str) -> frozenset[str] | None:
    """判断本次转发是否需要按「可见计划白名单」过滤，需要时返回该白名单。

    仅针对计划**列表**接口：引擎按 ID 取单条计划的接口已在鉴权中间件按 404 拦掉，
    但列表接口不在计划作用域路径上，若不在此过滤，访客会看到全部计划的需求原文摘要。
    """
    principal = getattr(request.state, "principal", None)
    if principal is None or principal.plan_scope != PLAN_SCOPE_ASSIGNED:
        return None
    if request.method != "GET" or path.rstrip("/") != f"{base}/plans":
        return None
    return frozenset(principal.plan_ids)


def _filter_plan_list(content: bytes, visible: frozenset[str]) -> bytes | None:
    """从 ``{data: PlanSummary[]}`` 信封中剔除未授权计划；解析失败返回 ``None``（不改动）。"""
    try:
        envelope = json.loads(content)
    except (ValueError, TypeError):
        return None
    if not isinstance(envelope, dict) or not isinstance(envelope.get("data"), list):
        return None
    envelope["data"] = [item for item in envelope["data"] if _plan_id_of(item) in visible]
    return json.dumps(envelope, ensure_ascii=False).encode("utf-8")


def _plan_id_of(item: object) -> str:
    if isinstance(item, dict):
        value = item.get("planId")
        if isinstance(value, str):
            return value
    return ""


def _job_kind(path: str, base: str) -> str | None:
    """判断该路径是否属于可异步化的长耗时写；是则返回作业种类。

    只登记「引擎侧同步阻塞且耗时可观」的写接口：把普通快接口也做成作业，只会让调用方
    多两次往返，没有任何收益。
    """
    if not path.startswith(base):
        return None
    rel = path[len(base) :] or "/"
    if rel in _ASYNC_JOB_PATHS:
        return _ASYNC_JOB_PATHS[rel]
    if _REPLAN_PATH.match(rel):
        return JOB_KIND_PLAN_REPLAN
    return None


def _wants_async(request: Request) -> bool:
    """是否要求异步受理（``Prefer: respond-async``，RFC 7240）。

    首部可含多个偏好（逗号分隔），因此不能整串比较，必须按偏好逐项解析。
    """
    raw = request.headers.get(PREFER_HEADER, "")
    if not raw:
        return False
    return any(token.strip().lower().startswith(PREFER_RESPOND_ASYNC) for token in raw.split(","))


def _audit(event: str, request: Request, **fields: Any) -> None:
    """写链路审计日志（结构化字段，与作业 / 幂等审计同款，便于按请求号与用户检索）。

    作业与幂等的审计各自落在其归属模块（它们掌握生命周期事实）；这里只记**HTTP 边界**
    的事实——受理了哪个作业、同步长耗时写最终以何状态码返回，都是模块内看不到的。
    """
    from ..logging_config import request_id_var

    principal = getattr(request.state, "principal", None)
    logger.info(
        "写链路审计：%s",
        event,
        extra={
            "extra_fields": {
                "audit": event,
                "requestId": request_id_var.get() or "",
                "user": getattr(principal, "user_id", "") if principal is not None else "",
                "method": request.method,
                "path": request.url.path,
                **fields,
            }
        },
    )


def _error_response(exc: ApiError) -> JSONResponse:
    """标准错误信封 → 响应（首部随异常下发：``Proxy-Status`` / ``Retry-After`` 等）。"""
    return JSONResponse(status_code=exc.status_code, content=exc.to_body(), headers=exc.headers or None)


def _mark_replayed(response: Response) -> Response:
    """标注本次响应为**回放**（引擎未重复执行）。

    用 ``Idempotency-Replayed: true`` 而非自定义状态串：这是 IETF
    `draft-ietf-httpapi-idempotency-key-header` 约定的表达，调用方与中间网关无需额外约定即可识别。
    """
    response.headers["Idempotency-Replayed"] = "true"
    return response


def _mark_skipped(response: Response) -> Response:
    """标注本次**未受幂等保护**（存储故障时按草案要求放行并显式暴露）。

    必须显式暴露——否则调用方无法区分「真的只执行了一次」与「保护失效、可能执行了多次」。
    """
    response.headers["Idempotency-Status"] = "skipped"
    return response


async def _accept_async_job(ctx, request: Request, path: str, body: bytes, kind: str) -> Response:
    """受理长耗时写为异步作业：``202 Accepted`` + 作业资源地址。

    响应契约（对齐 Google LRO / Dify / Airflow 的共同做法）：返回作业 ID 与两个可
    直达的地址，调用方据此轮询或订阅进度，**不再把连接挂到算法计算结束**。
    """
    principal = getattr(request.state, "principal", None)
    base = ctx.settings.api_base_path
    try:
        record = await submit_job(
            ctx,
            kind=kind,
            owner=principal.user_id if principal is not None else "",
            method=request.method,
            path=path,
            body=body,
            params=[[key, value] for key, value in request.query_params.multi_items()],
            headers=_forward_headers(request),
        )
    except ApiError as exc:  # 作业存储不可用（503）/ 载荷超限（413）：标准错误信封
        return JSONResponse(status_code=exc.status_code, content=exc.to_body(), headers=exc.headers or None)

    job_id = record.jobId
    location = f"{base}/jobs/{job_id}"
    _audit("write.async.accepted", request, jobId=job_id, kind=kind)
    return JSONResponse(
        status_code=202,
        content={
            "data": {
                "jobId": job_id,
                "kind": kind,
                "status": record.status,
                "pollUrl": location,
                "streamUrl": f"{location}/stream",
                "createdAt": record.createdAt,
            }
        },
        headers={
            # Location 指向作业资源；Prefer 回显告知「已按异步受理」，
            # 调用方无需再自行推断本次是按同步还是异步语义处理的
            "Location": location,
            PREFER_HEADER: PREFER_RESPOND_ASYNC,
        },
    )


async def _forward(ctx, request: Request, path: str) -> Response:
    """把请求透传给内部算法引擎（长耗时写可异步化，带幂等键的写请求可防重放）。"""
    body = await request.body()
    params = list(request.query_params.multi_items())
    headers = _forward_headers(request)
    kind = _job_kind(path, ctx.settings.api_base_path)

    async def _produce() -> Response:
        if kind and request.method == "POST" and _wants_async(request):
            return await _accept_async_job(ctx, request, path, body, kind)
        return await _produce_engine(ctx, request, path, params, body, headers, long_running=kind is not None)

    raw_key = request.headers.get(IDEMPOTENCY_KEY_HEADER, "").strip()
    if not is_idempotent_method(request.method) or not raw_key:
        return await _produce()

    try:
        validate_key(raw_key)
        principal = getattr(request.state, "principal", None)
        return await run_idempotent(
            ctx.idempotency,
            owner=principal.user_id if principal is not None else "",
            method=request.method,
            path=path,
            params=params,
            body=body,
            raw_key=raw_key,
            producer=_produce,
            on_replay=_mark_replayed,
            on_degraded=_mark_skipped,
        )
    except ApiError as exc:  # 键过长（400）/ 同键异载荷（422）/ 同键处理中（409）
        return _error_response(exc)


async def _produce_engine(
    ctx,
    request: Request,
    path: str,
    params: list[tuple[str, str]],
    body: bytes,
    headers: dict[str, str],
    *,
    long_running: bool,
) -> Response:
    """真正转发到引擎并镜像响应（含访客可见性过滤）。"""
    started = time.monotonic()
    try:
        upstream = await ctx.engine.forward(
            request.method,
            path,
            params=params,
            content=body or None,
            headers=headers,
            # 长耗时写走同步路径时，必须放宽到「引擎真正的预算」，而不是普通接口的短超时：
            # 短超时 + 引擎不中断上游 = 客户端报失败、引擎已落库的孤儿写。
            timeout=ctx.settings.engine_long_timeout_seconds if long_running else None,
        )
    except ApiError as exc:  # 引擎不可达 / 超时 / 熔断：返回标准错误信封（含 Proxy-Status 等首部）
        if long_running:
            _audit(
                "write.sync.failed",
                request,
                error=exc.code,
                durationMs=int((time.monotonic() - started) * 1000),
            )
        return _error_response(exc)
    except Exception:  # noqa: BLE001 - 兜底，绝不抛出未捕获异常
        # 只回通用文案：``{exc}`` 可能带出引擎地址、连接串等内部细节，而这里是 catch-all
        # 路由，任何未转换的异常都会走到这——最不该成为信息泄漏面。细节留在服务端日志，
        # 调用方用响应头 ``X-Request-Id`` 关联排查。
        logger.exception("转发引擎请求失败：%s %s", request.method, path)
        if long_running:
            _audit(
                "write.sync.failed",
                request,
                error="ENGINE_UNAVAILABLE",
                durationMs=int((time.monotonic() - started) * 1000),
            )
        return JSONResponse(
            status_code=502,
            content={"code": "ENGINE_UNAVAILABLE", "message": "算法引擎暂不可达，请稍后重试"},
        )

    if long_running:
        # 同步长耗时写的收尾事实：状态码 + 实耗。作业路径下这条路走不到（作业有自己的审计），
        # 因此这里是「没走异步语义的调用方」唯一的服务端留痕。
        _audit(
            "write.sync.completed",
            request,
            status=upstream.status_code,
            durationMs=int((time.monotonic() - started) * 1000),
        )

    content = upstream.content
    visible = _plan_scope_filter(request, path, ctx.settings.api_base_path)
    if visible is not None and upstream.status_code < 400 and content:
        filtered = _filter_plan_list(content, visible)
        if filtered is not None:
            content = filtered

    if upstream.status_code == 204 or not content:
        return Response(status_code=upstream.status_code, headers=_response_headers(upstream))
    return Response(
        content=content,
        status_code=upstream.status_code,
        headers=_response_headers(upstream),
        media_type=upstream.headers.get("content-type"),
    )


def _serve_static(static_dir: str, full_path: str, request: Request) -> Response:
    """托管前端 SPA：命中文件则返回文件，否则 history 路由回落 index.html。"""
    if request.method != "GET":
        return JSONResponse(status_code=404, content={"code": "NOT_FOUND", "message": f"路由不存在：{full_path}"})

    if not static_dir or not os.path.isdir(static_dir):
        return JSONResponse(
            status_code=404,
            content={
                "code": "NOT_FOUND",
                "message": "未配置前端静态资源目录（WEB_STATIC_DIR）；API 路由请访问 /api/**",
            },
        )

    root = os.path.abspath(static_dir)
    candidate = os.path.abspath(os.path.join(root, full_path))
    # 防目录穿越：规范化后必须仍位于 static_dir 内
    if (candidate == root or candidate.startswith(root + os.sep)) and os.path.isfile(candidate):
        return FileResponse(candidate)

    index = os.path.join(root, "index.html")
    if os.path.isfile(index):
        return FileResponse(index, media_type="text/html")
    return JSONResponse(
        status_code=404,
        content={"code": "NOT_FOUND", "message": "前端产物不存在，请先执行 npm run web:build"},
    )


__all__ = ["register_proxy_and_static"]
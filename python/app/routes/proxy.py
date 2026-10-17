"""对外网关：把 ``/api/**`` 透传给内部算法引擎，并托管前端 SPA 静态资源。

选择「透传」而非「重写」的原因：保证 Vue 控制台面对 Python 服务时零改动 ——
响应信封（``{data}`` / ``{code,message,details}``）、状态码、导出文件全部原样镜像。
"""

from __future__ import annotations

import json
import logging
import os

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from ..constants import PLAN_SCOPE_ASSIGNED
from ..errors import ApiError

logger = logging.getLogger("iteration_planner.gateway")

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


async def _forward(ctx, request: Request, path: str) -> Response:
    """把请求透传给内部算法引擎。"""
    body = await request.body()
    params = list(request.query_params.multi_items())
    headers = _forward_headers(request)

    try:
        upstream = await ctx.engine.forward(
            request.method,
            path,
            params=params,
            content=body or None,
            headers=headers,
        )
    except ApiError as exc:  # 引擎不可达 / 熔断：返回标准错误信封（保持错误码语义）
        return JSONResponse(status_code=exc.status_code, content=exc.to_body())
    except Exception:  # noqa: BLE001 - 兜底，绝不抛出未捕获异常
        # 只回通用文案：``{exc}`` 可能带出引擎地址、连接串等内部细节，而这里是 catch-all
        # 路由，任何未转换的异常都会走到这——最不该成为信息泄漏面。细节留在服务端日志，
        # 调用方用响应头 ``X-Request-Id`` 关联排查。
        logger.exception("转发引擎请求失败：%s %s", request.method, path)
        return JSONResponse(
            status_code=502,
            content={"code": "ENGINE_UNAVAILABLE", "message": "算法引擎暂不可达，请稍后重试"},
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
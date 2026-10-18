"""异步作业端点（``{base}/jobs/*``）：长耗时写的「状态资源」与进度流。

作业的来龙去脉见 :mod:`app.jobs`。本模块只把作业记录暴露成标准资源：

- ``GET    {base}/jobs/{id}``         作业快照（状态 / 进度 / 结果）
- ``GET    {base}/jobs/{id}/stream``  SSE 进度流（``progress`` → ``done`` / ``error``）
- ``POST   {base}/jobs/{id}/cancel``  取消作业（尽力而为，语义见 :mod:`app.jobs`）

**可见性**：作业按 ``owner`` 隔离，非属主访问一律 **404**（而非 403）——否则可以借
状态码差异探测「别人是否存在这个作业」。这也与计划越权访问的处理口径一致。

**进度流实现**：以「轮询状态 + 增量下发」实现，而不是为作业再引入一套消息总线
（Redis Pub/Sub / Streams）。理由：作业本身就是状态资源，轮询语义天然跨副本正确；
引入消息总线会再增加一条「推送断了怎么办」的可靠性缺口，而这里的最坏后果只是进度
晚 1 秒送达。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ..constants import (
    JOB_STATUS_FAILED,
    SSE_DONE,
    SSE_ERROR,
    SSE_HEARTBEAT_SECONDS,
    SSE_PROGRESS,
)
from ..errors import job_not_found, job_store_unavailable
from ..jobs import KvStoreError, cancel_job
from ..state import AppContext

logger = logging.getLogger("iteration_planner.jobs.api")

#: 进度轮询间隔（秒）：作业阶段推进是分钟级的，1 秒轮询对存储的压力可忽略
_POLL_SECONDS = 1.0

#: 作业 ID 字符集白名单：作业 ID 会拼进存储键，必须限制字符集，避免键空间被注入
#: （本项目生成的 ID 恒为 ``uuid4().hex``，即 32 位小写十六进制）
_JOB_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")


def _sse_event(name: str, data: Any) -> str:
    """编码 SSE 帧（``data`` 单行 JSON，避免破坏帧边界）。"""
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {name}\ndata: {payload}\n\n"


def _sanitize_job_id(raw: str) -> str | None:
    """校验作业 ID；不合法返回 ``None``（由调用方按 404 处理，不区分「格式错」与「不存在」）。"""
    if not raw or not _JOB_ID_RE.match(raw):
        return None
    return raw.lower()


def _owner_of(request: Request) -> str:
    """当前请求的作业属主标识（鉴权中间件保证主体恒存在）。"""
    principal = getattr(request.state, "principal", None)
    return principal.user_id if principal is not None else ""


async def _load_for_owner(ctx: AppContext, job_id: str, owner: str):
    """按属主取作业；不存在 / 非属主 / 存储不可用分别抛标准错误。"""
    if ctx.jobs is None:
        raise job_store_unavailable()
    try:
        record = await ctx.jobs.get(job_id)
    except KvStoreError as exc:
        logger.warning("作业存储读取失败：%s", exc)
        raise job_store_unavailable() from exc
    if record is None or record.owner != owner:
        raise job_not_found()
    return record


async def _job_events(ctx: AppContext, job_id: str, owner: str) -> AsyncIterator[str]:
    """作业进度事件源：增量下发新进度，终态以 ``done`` / ``error`` 收尾。"""
    emitted = 0
    last_output = time.monotonic()
    while True:
        try:
            record = await ctx.jobs.get(job_id)
        except KvStoreError as exc:
            # 流已建立（HTTP 200 已发出），存储故障只能以事件形式告知，不能改状态码
            logger.warning("作业流读取存储失败：%s", exc)
            yield _sse_event(SSE_ERROR, job_store_unavailable().to_body())
            return
        if record is None or record.owner != owner:
            # 作业过期 / 被清理：明确收尾，避免客户端无限等待
            yield _sse_event(SSE_ERROR, job_not_found().to_body())
            return

        fresh = record.progress[emitted:]
        if fresh:
            emitted += len(fresh)
            last_output = time.monotonic()
            for item in fresh:
                yield _sse_event(SSE_PROGRESS, {"jobId": job_id, "status": record.status, **item})

        if record.terminal:
            name = SSE_ERROR if record.status == JOB_STATUS_FAILED else SSE_DONE
            yield _sse_event(name, record.snapshot())
            return

        if time.monotonic() - last_output >= SSE_HEARTBEAT_SECONDS:
            # 注释行心跳：空闲超过阈值时保活，避免代理 / 网关按空闲断开长连接
            last_output = time.monotonic()
            yield ": ping\n\n"

        await asyncio.sleep(_POLL_SECONDS)


def build_jobs_router(base: str) -> APIRouter:
    router = APIRouter(prefix=f"{base}/jobs", tags=["jobs"])

    @router.get("/{job_id}", summary="作业快照（状态 / 进度 / 结果）")
    async def job_snapshot(job_id: str, request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        resolved = _sanitize_job_id(job_id)
        if resolved is None:
            raise job_not_found()
        record = await _load_for_owner(ctx, resolved, _owner_of(request))
        return {"data": record.snapshot()}

    @router.get("/{job_id}/stream", summary="作业进度流（SSE：progress → done / error）")
    async def job_stream(job_id: str, request: Request) -> StreamingResponse:
        ctx: AppContext = request.app.state.ctx
        resolved = _sanitize_job_id(job_id)
        if resolved is None:
            raise job_not_found()
        owner = _owner_of(request)
        # 可见性校验必须在建立流之前完成：流一旦开始（200 已发出）就无法再改状态码
        await _load_for_owner(ctx, resolved, owner)
        return StreamingResponse(
            _job_events(ctx, resolved, owner),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @router.post("/{job_id}/cancel", summary="取消作业（尽力而为）")
    async def job_cancel(job_id: str, request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        resolved = _sanitize_job_id(job_id)
        if resolved is None:
            raise job_not_found()
        try:
            record = await cancel_job(ctx, resolved, owner=_owner_of(request))
        except KvStoreError as exc:
            logger.warning("作业存储写入失败（取消）：%s", exc)
            raise job_store_unavailable() from exc
        return {"data": record.snapshot()}

    return router


__all__ = ["build_jobs_router"]

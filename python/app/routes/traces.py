"""Agent 执行轨迹端点（``{base}/agent/traces/*``）。

一次 Agent 运行结束后，光有终态结论无法回答「它当时到底做了什么」。本组端点把
:mod:`app.traces` 落库的轨迹暴露为标准资源：

- ``GET {base}/agent/traces``          本主体最近的轨迹列表（仅摘要，不含 span 明细）
- ``GET {base}/agent/traces/{id}``     轨迹全量（span 时序 + 终态），前端渲染调试器式瀑布图，
  并可按 ``request`` 原样回放这次运行
- ``GET {base}/agent/traces/{id}/replay``  回放参数（原始请求 + 终态），供前端一键重跑

**可见性**：轨迹按 ``owner`` 隔离，非属主一律 **404**（与台账、作业、计划同一口径）——
轨迹里含用户原始需求与工具返回数据，不能靠状态码差异被第三方探测。

**失败语义**：轨迹是**观测设施**，与台账不同。读取失败时同样显式报错（
``TRACE_STORE_UNAVAILABLE`` → 503）而不是静默返回空列表：空列表会被误读成
「这次运行没有留下轨迹」，从而让人放弃排障。

事故沉淀（把失败轨迹变成回归用例）写在评测台路由里（``POST {base}/evals/incidents``）：
用例属于评测资源，和评测台其它内容同类，放在一起才不会让「用例存在哪」变成两处口径。
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Request

from ..constants import TRACE_KINDS, TRACE_RUN_STATUSES, TRACE_STATUSES
from ..errors import ApiError
from ..kvstore import KvStoreError
from ..models import TraceListResponse
from ..state import AppContext
from ..traces import summarize_trace

logger = logging.getLogger("iteration_planner.trace.api")

#: 轨迹 ID 恒为 ``uuid4().hex``（32 位十六进制）：会拼进存储键，必须限制字符集，
#: 否则路径参数可被用来构造任意键（键空间注入）
_TRACE_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")

_LIST_DEFAULT_LIMIT = 20
_LIST_MAX_LIMIT = 100


def _trace_store(ctx: AppContext):
    if ctx.traces is None:
        raise ApiError(503, "TRACE_STORE_UNAVAILABLE", "执行轨迹未启用（AGENT_TRACE_ENABLED=false）")
    return ctx.traces


def _owner_of(request: Request) -> str:
    principal = getattr(request.state, "principal", None)
    return principal.user_id if principal is not None else ""


def _trace_not_found() -> ApiError:
    """统一的「不存在」响应：格式非法与真不存在**不区分**，避免被用来探测他人轨迹。"""
    return ApiError(404, "TRACE_NOT_FOUND", "执行轨迹不存在")


async def _load(ctx: AppContext, trace_id: str, owner: str):
    """按属主取轨迹；存储故障 / 不存在 / 非属主分别抛标准错误。"""
    if not trace_id or not _TRACE_ID_RE.match(trace_id):
        raise _trace_not_found()
    store = _trace_store(ctx)
    try:
        trace = await store.get(trace_id.lower())
    except KvStoreError as exc:
        logger.warning("执行轨迹读取失败：%s", exc)
        raise ApiError(503, "TRACE_STORE_UNAVAILABLE", "执行轨迹存储暂不可用，请稍后重试") from exc
    if trace is None or trace.owner != owner:
        raise _trace_not_found()
    return store, trace


def build_traces_router(base: str) -> APIRouter:
    router = APIRouter(prefix=f"{base}/agent/traces", tags=["traces"])

    @router.get("", summary="执行轨迹列表（本主体最近的运行；仅摘要）")
    async def trace_list(
        request: Request, limit: int = _LIST_DEFAULT_LIMIT, kind: str | None = None
    ) -> dict:
        ctx: AppContext = request.app.state.ctx
        store = _trace_store(ctx)
        resolved = max(1, min(limit, _LIST_MAX_LIMIT))
        try:
            items = await store.list(_owner_of(request), limit=resolved, kind=kind or None)
        except KvStoreError as exc:
            logger.warning("执行轨迹列表读取失败：%s", exc)
            raise ApiError(503, "TRACE_STORE_UNAVAILABLE", "执行轨迹存储暂不可用，请稍后重试") from exc
        response = TraceListResponse(
            items=[summarize_trace(trace) for trace in items],
            total=len(items),
            enabled=True,
            backend=store.backend,
            kinds=list(TRACE_KINDS),
            statuses=list(TRACE_STATUSES),
        )
        return {"data": response.model_dump()}

    @router.get("/{trace_id}", summary="执行轨迹全量（span 时序 + 原始请求，可逐 step 回放）")
    async def trace_detail(trace_id: str, request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        _, trace = await _load(ctx, trace_id, _owner_of(request))
        return {"data": trace.model_dump()}

    @router.get("/{trace_id}/replay", summary="回放参数（原始请求 + 终态）")
    async def trace_replay(trace_id: str, request: Request) -> dict:
        """回放所需的**原始请求**（前端可据此一键重跑，事故复盘不必让人重新手写需求）。

        单独开一个端点而不是让前端从全量里自己抠：回放参数是**稳定契约**，
        与 span 明细（字段可能因观测需要而增减）解耦。
        """
        from ..evals.incidents import replay_payload

        ctx: AppContext = request.app.state.ctx
        _, trace = await _load(ctx, trace_id, _owner_of(request))
        return {
            "data": {
                **replay_payload(trace),
                "runStatuses": list(TRACE_RUN_STATUSES),
                "hasIncident": trace.hasIncident,
                "incidentCaseId": trace.incidentCaseId,
            }
        }

    return router


__all__ = ["build_traces_router"]

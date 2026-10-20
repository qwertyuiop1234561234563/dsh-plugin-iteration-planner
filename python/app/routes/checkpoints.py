"""检查点台账端点（``{base}/agent/checkpoints/*``）。

把「两阶段写确认」的第一阶段产物从**易失的会话内存**升级为**持久化运行台账**之后，
前端（以及排障的同学）需要一组能真正操作它的资源端点：

- ``GET    {base}/agent/checkpoints``           本主体最近的台账列表（仅摘要，不含正文）
- ``GET    {base}/agent/checkpoints/{id}``      台账快照（含请求 / 待确认动作 / 执行结果 / 反思轨迹）
- ``POST   {base}/agent/checkpoints/{id}/resume``  续跑：确认落库 / 按原请求重跑 / 驳回
- ``DELETE {base}/agent/checkpoints/{id}``      作废并移除台账（丢弃待确认动作，**不执行**任何写入）

**可见性**：台账按 ``owner`` 隔离，非属主访问一律 **404**（而非 403）——与作业、计划越权
访问同一口径，避免借状态码差异探测「别人是否有一条台账」。

**失败语义**：台账是**状态数据**（见 :mod:`app.checkpoints`），存储故障必须显式失败
（``CHECKPOINT_STORE_UNAVAILABLE`` → 503），绝不静默降级成「查不到」——否则用户会误判
「我的待确认动作丢了」，而实际只是后端抖了一下。
"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Request

from ..agent.resume import CheckpointResumer
from ..checkpoints import CheckpointStore
from ..constants import CHECKPOINT_STATUSES
from ..errors import checkpoint_not_found, checkpoint_store_unavailable
from ..kvstore import KvStoreError
from ..models import (
    AgentCheckpoint,
    CheckpointListResponse,
    CheckpointResumeRequest,
    CheckpointSummary,
)
from ..state import AppContext

logger = logging.getLogger("iteration_planner.checkpoint.api")

#: 台账 ID 恒为 ``uuid4().hex``（32 位十六进制）——会拼进存储键，必须限制字符集，
#: 否则路径参数可被用来构造任意键（键空间注入）
_CHECKPOINT_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")

#: 列表默认 / 最大返回条数（列表响应必须收敛，避免提交多轮后一次拉回上千条）
_LIST_DEFAULT_LIMIT = 20
_LIST_MAX_LIMIT = 100


def _sanitize_checkpoint_id(raw: str) -> str | None:
    """校验台账 ID；不合法返回 ``None``（由调用方按 404 处理，不区分「格式错」与「不存在」）。"""
    if not raw or not _CHECKPOINT_ID_RE.match(raw):
        return None
    return raw.lower()


def _owner_of(request: Request) -> str:
    """当前请求的主体标识（鉴权中间件保证主体恒存在）。"""
    principal = getattr(request.state, "principal", None)
    return principal.user_id if principal is not None else ""


def _store(ctx: AppContext) -> CheckpointStore:
    """取台账存储；未启用（``CHECKPOINT_STORE=disabled`` 场景）按不可用处理。"""
    if ctx.checkpoints is None:
        raise checkpoint_store_unavailable("检查点台账未启用")
    return ctx.checkpoints


async def _load(ctx: AppContext, checkpoint_id: str, owner: str) -> tuple[CheckpointStore, AgentCheckpoint]:
    """按属主取台账；存储故障 / 不存在 / 非属主分别抛标准错误。"""
    store = _store(ctx)
    try:
        checkpoint = await store.get(checkpoint_id)
    except KvStoreError as exc:
        logger.warning("检查点台账读取失败：%s", exc)
        raise checkpoint_store_unavailable() from exc
    if checkpoint is None or checkpoint.owner != owner:
        raise checkpoint_not_found()
    return store, checkpoint


def build_checkpoints_router(base: str) -> APIRouter:
    router = APIRouter(prefix=f"{base}/agent/checkpoints", tags=["checkpoints"])

    @router.get("", summary="检查点台账列表（本主体最近的待确认 / 可续跑运行）")
    async def checkpoint_list(request: Request, limit: int = _LIST_DEFAULT_LIMIT) -> dict:
        ctx: AppContext = request.app.state.ctx
        store = _store(ctx)
        resolved = max(1, min(limit, _LIST_MAX_LIMIT))
        try:
            items = await store.list(_owner_of(request), limit=resolved)
        except KvStoreError as exc:
            logger.warning("检查点台账列表读取失败：%s", exc)
            raise checkpoint_store_unavailable() from exc
        response = CheckpointListResponse(
            items=[
                CheckpointSummary(
                    checkpointId=item.checkpointId,
                    sessionId=item.sessionId,
                    kind=item.kind,
                    status=item.status,
                    attempt=item.attempt,
                    planId=item.planId,
                    pendingCount=len(item.pendingActions),
                    updatedAt=item.updatedAt,
                    resumable=item.resumable,
                )
                for item in items
            ],
            total=len(items),
            enabled=True,
            backend=store.backend,
            statuses=list(CHECKPOINT_STATUSES),
        )
        return {"data": response.model_dump()}

    @router.get("/{checkpoint_id}", summary="检查点台账快照（请求 / 待确认动作 / 执行结果 / 反思轨迹）")
    async def checkpoint_snapshot(checkpoint_id: str, request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        resolved = _sanitize_checkpoint_id(checkpoint_id)
        if resolved is None:
            raise checkpoint_not_found()
        _, checkpoint = await _load(ctx, resolved, _owner_of(request))
        return {"data": checkpoint.model_dump()}

    @router.post("/{checkpoint_id}/resume", summary="续跑检查点（确认落库 / 按原请求重跑 / 驳回）")
    async def checkpoint_resume(
        checkpoint_id: str, payload: CheckpointResumeRequest, request: Request
    ) -> dict:
        """在台账上继续一次被中断的编排（语义见 :mod:`app.agent.resume`）。

        ``action=auto`` 按台账当前状态自动分流：``awaiting_approval`` → 确认落库；
        ``running`` / ``failed`` → 用**台账里保存的原始请求**重跑（不需要用户重新描述需求）。
        确认是幂等的：已终态台账再次确认返回上次快照并标记 ``replayed=true``，不会二次写库。
        """
        ctx: AppContext = request.app.state.ctx
        resolved = _sanitize_checkpoint_id(checkpoint_id)
        if resolved is None:
            raise checkpoint_not_found()
        store, checkpoint = await _load(ctx, resolved, _owner_of(request))

        resumer = CheckpointResumer(
            store=store,
            runner=ctx.runner,
            sessions=ctx.store,
            max_attempts=ctx.settings.checkpoint_max_attempts,
        )
        try:
            result = await resumer.resume(
                checkpoint,
                action=payload.action,
                action_ids=payload.actionIds,
                reject=payload.reject,
            )
        except KvStoreError as exc:
            logger.warning("检查点台账写入失败（续跑）：%s", exc)
            raise checkpoint_store_unavailable() from exc
        return {"data": result.model_dump()}

    @router.delete("/{checkpoint_id}", summary="作废检查点（丢弃待确认动作并移除台账）")
    async def checkpoint_delete(checkpoint_id: str, request: Request) -> dict:
        """作废台账并移除记录：**只丢弃待确认动作，绝不顺带执行它们**。

        与 MCP 侧 ``DELETE`` 终止会话同一语义：调用方主动删除的含义就是「我不要这批改动了」。
        """
        ctx: AppContext = request.app.state.ctx
        resolved = _sanitize_checkpoint_id(checkpoint_id)
        if resolved is None:
            raise checkpoint_not_found()
        store, checkpoint = await _load(ctx, resolved, _owner_of(request))

        resumer = CheckpointResumer(
            store=store,
            runner=ctx.runner,
            sessions=ctx.store,
            max_attempts=ctx.settings.checkpoint_max_attempts,
        )
        try:
            result = await resumer.resume(checkpoint, action="abort")
            await store.delete(resolved)
        except KvStoreError as exc:
            logger.warning("检查点台账删除失败：%s", exc)
            raise checkpoint_store_unavailable() from exc
        return {"data": {**result.checkpoint.model_dump(), "deleted": True}}

    return router


__all__ = ["build_checkpoints_router"]

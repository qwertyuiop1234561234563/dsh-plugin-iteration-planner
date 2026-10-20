"""持久化检查点（两阶段写确认的**耐用化** + 断点续跑）。

问题：原先「待确认写操作」只活在会话存储里（``SessionStore.set_pending``）。而会话存储
默认是进程内存 —— 进程重启（发布、崩溃、扩容）后待确认动作直接蒸发，用户点确认只会得到
「没有待确认的操作」；长耗时编排中途失败也只能从头重来。

本模块把第一阶段的产物升级为**持久化运行台账**（checkpoint）：

- **durable**：落在 :mod:`app.kvstore`（内存 / Redis 可插拔）。多副本部署切 Redis 后，
  任一副本都能读到同一份台账 —— 用户在 A 副本登记、到 B 副本确认，照样能落库。
- **断点续跑**：台账里存了「原始请求 + 待确认动作 + 执行结果」。进程重启后按 ``checkpointId``
  即可继续确认（``awaiting_approval`` → 落库）或按原请求重跑（``running`` / ``failed``）。
- **幂等**：续跑带 ``attempt`` 计数与上限（配置项 ``CHECKPOINT_MAX_ATTEMPTS``，默认 3），
  且落库动作经 :class:`app.idempotency.IdempotencyGuard` 原子抢占，重复提交返回上次结果
  （``replayed=True``）而非二次写入。

**只在值得续跑时才建台账**（有待确认动作 / 编排失败）：只读问答既没有待确认动作也没有
需要重跑的半成品，为它落盘只是白白增加每轮对话的存储写入。这条取舍此处显式记录，避免
后来者「顺手」给所有请求都建台账。

键值存储故障语义沿用 :mod:`app.kvstore`：台账是**状态数据**，后端不可用时显式失败
（映射为 503），不静默降级成「各副本各存各的」。
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from .constants import (
    CHECKPOINT_KINDS,
    CHECKPOINT_PREFIX,
    CHECKPOINT_RESUMABLE_STATUSES,
    CHECKPOINT_STATUS_AWAITING,
    CHECKPOINT_STATUSES,
)
from .kvstore import KeyValueStore, build_kv_store
from .metrics import METRICS
from .models import AgentCheckpoint, CheckpointRequest

logger = logging.getLogger("iteration_planner.checkpoint")

#: 全局索引（管理员可见全部台账）
_INDEX_ALL = "*"
#: 单主体索引保留条数上限（防止索引本身无限增长）
_MAX_OWNER_INDEX = 100
#: 单会话索引保留条数上限（会话只关心「最近一次」可续跑台账）
_MAX_SESSION_INDEX = 10


class CheckpointStore:
    """检查点台账存储（构建在可插拔键值存储之上）。"""

    def __init__(self, kv: KeyValueStore, *, ttl_seconds: float) -> None:
        self._kv = kv
        self._ttl = ttl_seconds

    @property
    def backend(self) -> str:
        return self._kv.name

    def describe(self) -> dict[str, Any]:
        return {"statuses": list(CHECKPOINT_STATUSES), "kinds": list(CHECKPOINT_KINDS), **self._kv.describe()}

    async def close(self) -> None:
        await self._kv.close()

    # ------------------------------- 读写 -------------------------------

    async def create(
        self,
        *,
        session_id: str,
        owner: str,
        kind: str,
        request: CheckpointRequest,
        status: str,
        pending_actions: list[dict[str, Any]] | None = None,
        executed: list[dict[str, Any]] | None = None,
        content: str = "",
        plan_id: str | None = None,
        steps: int = 0,
        tool_calls: list[dict[str, Any]] | None = None,
        reflections: list[dict[str, Any]] | None = None,
        error: str | None = None,
    ) -> AgentCheckpoint:
        """登记一个新的运行台账（``attempt`` 从 1 开始）。"""
        now = time.time()
        checkpoint = AgentCheckpoint(
            checkpointId=uuid.uuid4().hex,
            sessionId=session_id,
            owner=owner or "anonymous",
            kind=kind,
            status=status,
            attempt=1,
            request=request,
            pendingActions=pending_actions or [],
            executed=executed or [],
            content=content,
            planId=plan_id,
            steps=steps,
            toolCalls=tool_calls or [],
            reflections=reflections or [],
            error=error,
            createdAt=now,
            updatedAt=now,
            resumable=status in CHECKPOINT_RESUMABLE_STATUSES,
        )
        await self.save(checkpoint)
        METRICS.inc_counter("checkpoint_operations_total", {"operation": "create", "status": status})
        return checkpoint

    async def get(self, checkpoint_id: str) -> AgentCheckpoint | None:
        raw = await self._kv.get(self._key(checkpoint_id))
        if not isinstance(raw, dict):
            METRICS.inc_counter("checkpoint_operations_total", {"operation": "get", "status": "miss"})
            return None
        try:
            checkpoint = AgentCheckpoint.model_validate(raw)
        except Exception:  # noqa: BLE001 - 脏数据（版本不符）按不存在处理，由调用方重建
            logger.warning("检查点 %s 结构非法，已按不存在处理", checkpoint_id)
            METRICS.inc_counter("checkpoint_operations_total", {"operation": "get", "status": "corrupt"})
            return None
        checkpoint.resumable = checkpoint.status in CHECKPOINT_RESUMABLE_STATUSES
        METRICS.inc_counter("checkpoint_operations_total", {"operation": "get", "status": "hit"})
        return checkpoint

    async def save(self, checkpoint: AgentCheckpoint) -> AgentCheckpoint:
        """写入台账（并维护主体 / 会话 / 全局索引）。"""
        checkpoint.updatedAt = time.time()
        checkpoint.resumable = checkpoint.status in CHECKPOINT_RESUMABLE_STATUSES
        await self._kv.set(self._key(checkpoint.checkpointId), checkpoint.model_dump(), self._ttl)
        await self._touch_index(self._owner_index_key(checkpoint.owner), checkpoint.checkpointId, _MAX_OWNER_INDEX)
        if checkpoint.owner != "anonymous":
            await self._touch_index(self._owner_index_key(_INDEX_ALL), checkpoint.checkpointId, _MAX_OWNER_INDEX)
        if checkpoint.sessionId:
            # 端到端规划（kind=plan）没有会话，sessionId 为空：跳过会话索引，
            # 避免建出一堆 ``index:session:`` 的空键
            await self._touch_index(
                self._session_index_key(checkpoint.sessionId), checkpoint.checkpointId, _MAX_SESSION_INDEX
            )
        METRICS.inc_counter("checkpoint_operations_total", {"operation": "save", "status": checkpoint.status})
        return checkpoint

    async def list(self, owner: str | None = None, *, limit: int = 50) -> list[AgentCheckpoint]:
        """按主体筛选的台账列表（``owner=None`` 表示不筛选，返回全局最近台账）。"""
        key = self._owner_index_key(owner or _INDEX_ALL)
        ids = await self._read_index(key)
        if not ids and owner:
            # 主体索引缺失（首次访问 / 索引被 LRU 淘汰）：稳妥回落到全局索引再过滤，
            # 宁可多读几条，也不要让用户「查不到自己刚登记的台账」。
            ids = await self._read_index(self._owner_index_key(_INDEX_ALL))
        found: list[AgentCheckpoint] = []
        for checkpoint_id in ids:
            if len(found) >= limit:
                break
            checkpoint = await self.get(checkpoint_id)
            if checkpoint is None:
                continue
            if owner and checkpoint.owner != owner:
                continue
            found.append(checkpoint)
        return found

    async def latest_for_session(self, session_id: str, *, kind: str | None = None) -> AgentCheckpoint | None:
        """取该会话最近一次台账（供「按会话续跑」与前端自动恢复）。"""
        for checkpoint_id in await self._read_index(self._session_index_key(session_id)):
            checkpoint = await self.get(checkpoint_id)
            if checkpoint is None:
                continue
            if kind and checkpoint.kind != kind:
                continue
            return checkpoint
        return None

    async def pending_count(self, session_id: str) -> int:
        """该会话当前待确认动作总数（跨台账累计；用于 UI 提示「有未落库的改动」）。"""
        total = 0
        for checkpoint_id in await self._read_index(self._session_index_key(session_id)):
            checkpoint = await self.get(checkpoint_id)
            if checkpoint is not None and checkpoint.status == CHECKPOINT_STATUS_AWAITING:
                total += len(checkpoint.pendingActions)
        return total

    async def delete(self, checkpoint_id: str) -> None:
        await self._kv.delete(self._key(checkpoint_id))

    # ------------------------------- 内部实现 -------------------------------

    def _key(self, checkpoint_id: str) -> str:
        return f"cp:{checkpoint_id}"

    def _owner_index_key(self, owner: str) -> str:
        return f"index:owner:{owner}"

    def _session_index_key(self, session_id: str) -> str:
        return f"index:session:{session_id}"

    async def _touch_index(self, key: str, checkpoint_id: str, cap: int) -> None:
        """把台账 ID 插到索引头部（最近优先）并按上限裁剪。"""
        ids = await self._read_index(key)
        ids = [checkpoint_id, *[item for item in ids if item != checkpoint_id]][:cap]
        await self._kv.set(key, ids, self._ttl)

    async def _read_index(self, key: str) -> list[str]:
        raw = await self._kv.get(key)
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, str)]


def build_checkpoint_store(
    *,
    driver: str = "auto",
    redis_url: str = "",
    max_entries: int = 2048,
    ttl_seconds: float,
) -> CheckpointStore:
    """按配置构建检查点存储。

    ``driver=redis`` 但缺少 ``REDIS_URL`` 或未安装 ``redis`` 依赖时**启动即失败**：
    断点续跑的整个价值都建立在「多副本共享同一份台账」之上，静默降级为进程内存会让
    「在 A 副本登记、去 B 副本确认」变成必然失败的假象。
    """
    kv = build_kv_store(
        store="checkpoint",
        driver=driver,
        redis_url=redis_url,
        max_entries=max(max_entries, 512),
        prefix=CHECKPOINT_PREFIX,
    )
    return CheckpointStore(kv, ttl_seconds=ttl_seconds)


__all__ = ["CheckpointStore", "build_checkpoint_store"]

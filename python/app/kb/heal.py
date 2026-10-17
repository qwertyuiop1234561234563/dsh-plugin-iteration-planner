"""向量库索引自愈：应对 Chroma 持久化索引偶发不可读的上游缺陷。

**问题现象**（已在本仓库以 ~1.5% 的复现率定位并验证）：

- 新建的 Chroma 持久化集合在极少数情况下会缺失 HNSW 索引段，此后**任何**向量检索
  都会以 ``Error creating hnsw segment reader: Nothing found on disk`` 永久失败；
- 失败是「粘性」的：**重试无效**，**新建客户端重新打开同一路径也无效**；
- 但文档与元数据仍能正常读回（走 SQLite 路径，不经过索引段），因此**数据没有丢**。

**自愈策略**：把该集合的文档读回内存 → **先完成重新向量化** → 删除并重建集合 →
写回。向量化刻意放在删除之前，保证自愈中途失败时不会破坏原有数据。

工程约束：
- 只在命中上述**特定**错误特征时触发，其它错误一律原样抛出（不掩盖真实故障）；
- 同一进程内每个集合只尝试一次，避免反复付出重新向量化的代价；
- 单集合待重建块数超过 ``_MAX_HEAL_ROWS`` 时放弃自愈（避免巨额向量化开销），仅告警；
- 过程与结果均有指标（``vector_store_self_heal_total``）与结构化告警，便于运维告警；
- **同一集合串行重建**：重建跨越多个 ``await``（读取 → 向量化 → 删除 → 写回），并发请求
  可能同时对同一集合发起重建（后到者会把先到者刚写好的数据再删一次）。因此每个
  「存储实例 + 集合」配一把 ``asyncio.Lock``，并在进入临界区后**先重查一次**：若前一个
  协程已修好，直接返回结果，不重复重建。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..constants import KB_COLLECTION, MEMORY_COLLECTION
from ..errors import ApiError
from ..metrics import METRICS
from .embedder import Embedder
from .store import ChromaVectorStore

logger = logging.getLogger("iteration_planner.vectorstore")

#: 命中即判定为「索引段不可读」的日志特征（上游 Rust 内核原文）
_BROKEN_INDEX_SIGNATURES: tuple[str, ...] = (
    "hnsw segment reader",
    "Nothing found on disk",
)

#: 自愈时单集合可重建的块数上限
_MAX_HEAL_ROWS = 4000

#: 单个「存储实例 + 集合」允许的自愈尝试次数（防止在结构性损坏下反复付出重建代价）
_MAX_HEAL_ATTEMPTS = 2

#: 已消耗的自愈尝试次数：``{存储路径|集合: 次数}``
ATTEMPTS: dict[str, int] = {}

#: 每「存储实例 + 集合」一把重建互斥锁（条目数天然有界：同一存储最多 chunks / memories 两个键）
_HEAL_LOCKS: dict[str, asyncio.Lock] = {}


def is_broken_index_error(exc: BaseException) -> bool:
    """该异常是否为「索引段不可读」这一可自愈的上游缺陷。"""
    if not isinstance(exc, ApiError) or exc.code != "VECTOR_STORE_UNAVAILABLE":
        return False
    message = str(exc.message or "")
    return any(signature in message for signature in _BROKEN_INDEX_SIGNATURES)


async def query_resilient(
    store: ChromaVectorStore,
    embedder: Embedder,
    collection: str,
    *,
    embedding: list[float],
    top_k: int,
    where: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """带自愈的向量检索：正常路径与 ``store.query`` 完全一致，仅多一层索引段自愈。"""
    key = heal_key(store, collection)

    async def run() -> list[dict[str, Any]]:
        return await asyncio.to_thread(store.query, collection, embedding=embedding, top_k=top_k, where=where)

    try:
        return await run()
    except ApiError as exc:
        if not is_broken_index_error(exc):
            raise

    # 命中可自愈缺陷：进入「每集合串行」的临界区，避免并发请求同时重建同一集合
    async with _heal_lock(key):
        try:
            # 等待者在此重查：若已有协程把集合修好，直接返回，不再重复重建
            return await run()
        except ApiError as exc:
            if not is_broken_index_error(exc):
                raise
            if not should_attempt_heal(exc, key):
                raise
            METRICS.inc_counter("vector_store_self_heal_total", {"outcome": "attempted", "collection": collection})
            logger.warning(
                "检测到向量索引段不可读（collection=%s），尝试重建集合…：%s",
                collection,
                getattr(exc, "message", exc),
            )
            if not await rebuild_collection(store, embedder, collection):
                raise
    return await run()


def _heal_lock(key: str) -> asyncio.Lock:
    """取「存储实例 + 集合」维度的重建互斥锁（仅由事件循环内的协程调用，无需额外同步）。"""
    lock = _HEAL_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _HEAL_LOCKS[key] = lock
    return lock


def heal_key(store: ChromaVectorStore, collection: str) -> str:
    """自愈尝试配额的键：同一存储实例上的同一集合共享一份配额。"""
    return f"{getattr(store, 'path', '')}|{_canonical_collection(collection)}"


def should_attempt_heal(exc: BaseException, key: str) -> bool:
    """是否值得为该键再触发一次自愈（判定为「值得」时**同时消耗**一次配额）。

    配额按「存储实例 + 集合」计，而非按进程全局计：同一进程内可能存在多个
    互相独立的向量库实例（例如测试夹具），彼此的损坏互不相干。
    """
    if not is_broken_index_error(exc):
        return False
    if ATTEMPTS.get(key, 0) >= _MAX_HEAL_ATTEMPTS:
        return False
    ATTEMPTS[key] = ATTEMPTS.get(key, 0) + 1
    return True


async def rebuild_collection(store: ChromaVectorStore, embedder: Embedder, collection: str) -> bool:
    """读回文档 → 重新向量化 → 删除并重建集合 → 写回。返回是否重建成功。"""
    try:
        rows = await asyncio.to_thread(store.get, collection)
    except ApiError as exc:
        _record_heal("failed", "read_failed")
        logger.warning("向量索引自愈失败（读取原数据失败）：collection=%s %s", collection, exc.message)
        return False

    if not rows:
        # 空集合：检索本就无命中，无需重建
        _record_heal("skipped", "empty")
        return False
    if len(rows) > _MAX_HEAL_ROWS:
        _record_heal("skipped", "too_large")
        logger.warning(
            "向量索引自愈跳过：collection=%s 待重建块数 %d 超过上限 %d，请人工重建",
            collection,
            len(rows),
            _MAX_HEAL_ROWS,
        )
        return False

    documents = [str(row.get("document") or "") for row in rows]
    metadatas = [dict(row.get("metadata") or {}) for row in rows]
    ids = [str(row.get("id")) for row in rows]

    # 先向量化：失败时不触碰原集合，保证数据不丢
    try:
        embeddings = await embedder.embed(documents)
    except Exception as exc:  # noqa: BLE001 - 任意向量化失败都放弃自愈，保留原数据
        _record_heal("failed", "embed_failed")
        logger.warning("向量索引自愈失败（重新向量化失败，原集合未改动）：collection=%s %s", collection, exc)
        return False
    if len(embeddings) != len(rows):
        _record_heal("failed", "size_mismatch")
        logger.warning(
            "向量索引自愈失败（向量化条数不一致）：collection=%s rows=%d embeddings=%d",
            collection,
            len(rows),
            len(embeddings),
        )
        return False

    try:
        await asyncio.to_thread(store.reset, collection)
        await asyncio.to_thread(
            store.upsert,
            collection,
            ids=ids,
            documents=documents,
            embeddings=embeddings,
            metadatas=metadatas,
        )
    except ApiError as exc:
        _record_heal("failed", "rebuild_failed")
        logger.warning("向量索引自愈失败（重建写入失败）：collection=%s %s", collection, exc.message)
        return False

    _record_heal("success")
    logger.warning("向量索引自愈完成：collection=%s rows=%d", collection, len(rows))
    return True


def _record_heal(outcome: str, reason: str | None = None) -> None:
    labels = {"outcome": outcome}
    if reason:
        labels["reason"] = reason
    METRICS.inc_counter("vector_store_self_heal_total", labels)


def _canonical_collection(collection: str) -> str:
    """把集合别名归一为稳定标识（供「每集合只试一次」判定）。"""
    if collection in ("chunks", KB_COLLECTION):
        return "chunks"
    if collection in ("memories", MEMORY_COLLECTION):
        return "memories"
    return str(collection)


__all__ = [
    "ATTEMPTS",
    "heal_key",
    "is_broken_index_error",
    "query_resilient",
    "rebuild_collection",
    "should_attempt_heal",
]

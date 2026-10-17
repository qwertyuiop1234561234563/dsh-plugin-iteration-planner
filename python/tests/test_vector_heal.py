"""向量索引自愈（``app.kb.heal``）单元测试。

覆盖：命中缺陷特征时重建集合并恢复检索；其它错误不触发重建；重建前向量化失败
不破坏原集合；自愈尝试次数有上限；并发请求只重建一次；超过行数上限时放弃重建；
特征判定只认 ``ApiError`` + 特定报文。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.errors import vector_store_unavailable
from app.kb import heal
from app.metrics import METRICS
from tests.conftest import FailingEmbedder, StubEmbedder

BROKEN_MESSAGE = (
    "向量检索失败：Error executing plan: Internal error: Error creating hnsw segment reader: Nothing found on disk"
)


class FakeStore:
    """可编程向量库桩。

    ``query`` 在「损坏」状态下按预设错误抛错；``reset`` 默认把集合修好（重建即恢复），
    置 ``persistent_error=True`` 则模拟结构性损坏（重建后依然坏）。
    """

    path = "/tmp/fake-vector-store"

    def __init__(
        self,
        *,
        rows: list[dict[str, Any]] | None = None,
        error: Exception | None = None,
        persistent_error: bool = False,
    ) -> None:
        self._rows = rows or [{"id": "d:0000", "document": "关键路径按 AOE 计算", "metadata": {"docId": "d"}}]
        self._error = error or vector_store_unavailable(BROKEN_MESSAGE)
        self._persistent_error = persistent_error
        self._broken = True
        self.queries = 0
        self.resets = 0
        self.upserts: list[dict[str, Any]] = []

    # 与 ChromaVectorStore 对齐的同步接口（由 asyncio.to_thread 调用）
    def query(self, collection: str, *, embedding: list[float], top_k: int, where: dict[str, Any] | None = None):
        self.queries += 1
        if self._broken:
            raise self._error
        return list(self._rows)

    def get(self, collection: str, *, where: dict[str, Any] | None = None, include_documents: bool = True):
        return list(self._rows)

    def reset(self, collection: str) -> None:
        self.resets += 1
        self._broken = self._persistent_error

    def upsert(self, collection: str, *, ids, documents, embeddings, metadatas) -> None:
        self.upserts.append({"ids": list(ids), "documents": list(documents), "embeddings": list(embeddings)})


@pytest.fixture(autouse=True)
def _clear_heal_state():
    """每个用例前清空自愈配额，保证隔离。"""
    heal.ATTEMPTS.clear()
    yield
    heal.ATTEMPTS.clear()


async def test_recovers_from_broken_index():
    """命中索引段缺陷时应重建集合并恢复检索（一次自愈 + 一次重查）。"""
    store = FakeStore()
    rows = await heal.query_resilient(
        store, StubEmbedder(), "chunks", embedding=[0.1] * 64, top_k=3  # type: ignore[arg-type]
    )

    assert rows and rows[0]["id"] == "d:0000"
    # 首次检索失败 → 临界区内重查（仍失败）→ 重建 → 重建后检索成功
    assert store.queries == 3
    assert store.resets == 1
    assert store.upserts and store.upserts[0]["ids"] == ["d:0000"]
    assert heal.ATTEMPTS == {"/tmp/fake-vector-store|chunks": 1}

    rendered = METRICS.render()
    assert 'vector_store_self_heal_total{collection="chunks",outcome="attempted"}' in rendered
    assert "vector_store_self_heal_total" in rendered


async def test_other_vector_errors_are_not_healed():
    """非索引段缺陷的向量库错误必须原样抛出，不做任何重建。"""
    store = FakeStore(error=vector_store_unavailable("向量库读取失败：目录不可写"))

    with pytest.raises(Exception) as caught:  # noqa: B017 - 断言的是业务错误类型
        await heal.query_resilient(store, StubEmbedder(), "chunks", embedding=[0.1] * 64, top_k=3)  # type: ignore[arg-type]

    assert getattr(caught.value, "code", "") == "VECTOR_STORE_UNAVAILABLE"
    assert not heal.is_broken_index_error(caught.value)
    assert store.resets == 0
    assert heal.ATTEMPTS == {}


async def test_embedding_failure_keeps_collection_intact():
    """重建前向量化失败时必须放弃重建，绝不删除原集合（保证数据不丢）。"""
    store = FakeStore()

    with pytest.raises(Exception) as caught:  # noqa: B017
        await heal.query_resilient(
            store,
            FailingEmbedder(),  # type: ignore[arg-type]
            "chunks",
            embedding=[0.1] * 64,
            top_k=3,
        )

    assert getattr(caught.value, "code", "") == "VECTOR_STORE_UNAVAILABLE"
    assert store.resets == 0, "向量化失败时不应删除集合"
    assert store.upserts == []
    assert heal.ATTEMPTS == {"/tmp/fake-vector-store|chunks": 1}, "尝试配额应已被消耗（避免反复重试向量化）"


async def test_heal_attempts_are_capped_per_collection():
    """同一集合的自愈尝试次数有上限，超过后直接抛出，不再付出重建代价。"""
    store = FakeStore(persistent_error=True)
    for _ in range(heal._MAX_HEAL_ATTEMPTS):  # noqa: SLF001 - 直接校验配额上限
        with pytest.raises(Exception):  # noqa: B017
            await heal.query_resilient(store, StubEmbedder(), "chunks", embedding=[0.1] * 64, top_k=3)  # type: ignore[arg-type]
    assert store.resets == heal._MAX_HEAL_ATTEMPTS  # noqa: SLF001

    with pytest.raises(Exception):  # noqa: B017 - 配额耗尽后不再自愈
        await heal.query_resilient(store, StubEmbedder(), "chunks", embedding=[0.1] * 64, top_k=3)  # type: ignore[arg-type]
    assert store.resets == heal._MAX_HEAL_ATTEMPTS  # noqa: SLF001


async def test_attempt_budget_is_scoped_per_store():
    """尝试配额按「存储实例 + 集合」计：另一实例的损坏不应被前者的配额挡住。"""
    first = FakeStore()
    await heal.query_resilient(first, StubEmbedder(), "chunks", embedding=[0.1] * 64, top_k=3)  # type: ignore[arg-type]

    other = FakeStore()
    other.path = "/tmp/another-vector-store"
    rows = await heal.query_resilient(other, StubEmbedder(), "chunks", embedding=[0.1] * 64, top_k=3)  # type: ignore[arg-type]

    assert rows and other.resets == 1
    assert sorted(heal.ATTEMPTS) == ["/tmp/another-vector-store|chunks", "/tmp/fake-vector-store|chunks"]


class RecoverableStore:
    """重建（``reset``）后即恢复健康的向量库桩：用于验证并发下不会重复重建。"""

    path = "/tmp/fake-vector-store"

    def __init__(self) -> None:
        self.broken = True
        self.resets = 0
        self.upserts = 0

    def query(self, collection: str, *, embedding: list[float], top_k: int, where: dict[str, Any] | None = None):
        if self.broken:
            raise vector_store_unavailable(BROKEN_MESSAGE)
        return [{"id": "d:0000", "document": "关键路径按 AOE 计算", "metadata": {"docId": "d"}}]

    def get(self, collection: str, *, where: dict[str, Any] | None = None, include_documents: bool = True):
        return [{"id": "d:0000", "document": "关键路径按 AOE 计算", "metadata": {"docId": "d"}}]

    def reset(self, collection: str) -> None:
        self.resets += 1
        self.broken = False

    def upsert(self, collection: str, *, ids, documents, embeddings, metadatas) -> None:
        self.upserts += 1


async def test_concurrent_requests_rebuild_only_once():
    """并发检索命中同一缺陷时只能重建一次：后到者在临界区内重查即命中，不再重复重建。"""
    store = RecoverableStore()

    results = await asyncio.gather(
        heal.query_resilient(store, StubEmbedder(), "chunks", embedding=[0.1] * 64, top_k=3),  # type: ignore[arg-type]
        heal.query_resilient(store, StubEmbedder(), "chunks", embedding=[0.1] * 64, top_k=3),  # type: ignore[arg-type]
    )

    assert all(rows for rows in results)
    assert store.resets == 1, "并发下不得对同一集合重建两次（后到者会把先到者刚写好的数据再删一次）"
    assert store.upserts == 1
    assert heal.ATTEMPTS == {"/tmp/fake-vector-store|chunks": 1}


async def test_oversized_collection_skips_heal():
    """待重建块数超过上限时放弃自愈（避免巨额向量化开销），且绝不删除原集合。"""
    rows = [
        {"id": f"d:{index:05d}", "document": f"块 {index}", "metadata": {"docId": "d"}}
        for index in range(heal._MAX_HEAL_ROWS + 1)  # noqa: SLF001 - 直接校验阈值边界
    ]
    store = FakeStore(rows=rows)

    with pytest.raises(Exception) as caught:  # noqa: B017
        await heal.query_resilient(store, StubEmbedder(), "chunks", embedding=[0.1] * 64, top_k=3)  # type: ignore[arg-type]

    assert getattr(caught.value, "code", "") == "VECTOR_STORE_UNAVAILABLE"
    assert store.resets == 0, "超限跳过时不应删除集合"
    assert store.upserts == []
    rendered = METRICS.render()
    assert 'vector_store_self_heal_total{outcome="skipped",reason="too_large"}' in rendered


def test_is_broken_index_error_requires_api_error_and_signature():
    """特征判定必须同时满足「向量库不可用错误码」与「特定上游报文」。"""
    from app.errors import ApiError

    assert heal.is_broken_index_error(vector_store_unavailable(BROKEN_MESSAGE))
    # 非 ApiError 的同名报文（例如被包装成普通异常）不触发自愈
    assert not heal.is_broken_index_error(RuntimeError(BROKEN_MESSAGE))
    # 错误码不符（同为 5xx 但码不同）不触发
    assert not heal.is_broken_index_error(ApiError(500, "INTERNAL_ERROR", BROKEN_MESSAGE))
    # 错误码相符但报文不匹配（目录不可写等真实故障）不触发
    assert not heal.is_broken_index_error(vector_store_unavailable("向量库读取失败：目录不可写"))

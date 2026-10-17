"""会话存储与分布式锁用例。

覆盖企业级多副本场景下的关键不变量：

- 内存存储：TTL 过期、LRU 上限、会话级锁串行化；
- Redis 存储：跨「副本」（两个 store 实例）互斥、锁等待上限、token 安全释放、
  存储不可用时的明确错误码（不静默降级）、附属键不污染会话计数。

Redis 相关用例在无可用实例时自动跳过（``TEST_REDIS_URL`` 可覆盖默认地址）。
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from app.errors import ApiError
from app.sessions import InMemorySessionStore, RedisSessionStore, build_session_store

REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:6379/15")
TEST_PREFIX = "iteration_planner:test:"


# ------------------------------ 内存存储 ------------------------------


@pytest.mark.asyncio
async def test_inmemory_roundtrip_and_pending():
    store = InMemorySessionStore(max_sessions=4, ttl_seconds=60)
    assert await store.get("s1") is None
    await store.set("s1", [{"role": "user", "content": "hi"}])
    assert (await store.get("s1"))[0]["content"] == "hi"
    await store.set_pending("s1", [{"actionId": "a1"}])
    assert (await store.get_pending("s1"))[0]["actionId"] == "a1"
    await store.set_pending("s1", [])
    assert await store.get_pending("s1") is None
    assert await store.pop("s1") is True
    assert await store.pop("s1") is False


@pytest.mark.asyncio
async def test_inmemory_expires_and_evicts():
    store = InMemorySessionStore(max_sessions=2, ttl_seconds=0.05)
    await store.set("a", [{"role": "user", "content": "1"}])
    await asyncio.sleep(0.08)
    assert await store.get("a") is None, "TTL 过期后应视为不存在"

    store2 = InMemorySessionStore(max_sessions=2, ttl_seconds=60)
    for name in ("a", "b", "c"):
        await store2.set(name, [{"role": "user", "content": name}])
    assert await store2.size() == 2, "超出上限应淘汰最旧会话"
    assert await store2.get("a") is None and await store2.get("c") is not None


@pytest.mark.asyncio
async def test_inmemory_lock_serializes_same_session():
    store = InMemorySessionStore()
    order: list[str] = []

    async def worker(tag: str):
        async with store.lock("shared"):
            order.append("%s-in" % tag)
            await asyncio.sleep(0.05)
            order.append("%s-out" % tag)

    await asyncio.gather(worker("x"), worker("y"))
    assert order in (["x-in", "x-out", "y-in", "y-out"], ["y-in", "y-out", "x-in", "x-out"]), order
    assert store.lock("shared") is store.lock("shared"), "同一会话应复用同一把锁"


# ------------------------------ Redis 存储 ------------------------------


def _redis_store(**kwargs) -> RedisSessionStore:
    pytest.importorskip("redis")
    store = RedisSessionStore(REDIS_URL, ttl_seconds=60, prefix=TEST_PREFIX, **kwargs)
    return store


async def _cleanup(store: RedisSessionStore) -> None:
    async for key in store._redis.scan_iter(match=f"{TEST_PREFIX}*", count=100):
        await store._redis.delete(key)


@pytest.fixture
async def redis_store():
    store = _redis_store()
    try:
        await store._redis.ping()
    except Exception:  # pragma: no cover - 取决于本机是否有 Redis
        await store.close()
        pytest.skip("本机无可用 Redis，跳过分布式用例")
    await _cleanup(store)
    yield store
    await _cleanup(store)
    await store.close()


@pytest.mark.asyncio
async def test_redis_roundtrip_shares_across_instances(redis_store):
    replica_a = _redis_store()
    replica_b = _redis_store()
    try:
        await replica_a.set("shared", [{"role": "user", "content": "副本 A"}])
        history = await replica_b.get("shared")
        assert history and history[0]["content"] == "副本 A", "两个 store 实例应共享同一份会话"
        assert await replica_b.pop("shared") is True
        assert await replica_a.get("shared") is None, "删除应对所有副本可见"
    finally:
        await replica_a.close()
        await replica_b.close()


@pytest.mark.asyncio
async def test_redis_lock_excludes_across_instances(redis_store):
    """核心不变量：两个副本（两个 store 实例）对同一会话的临界区不得重叠。"""
    replica_a = _redis_store()
    replica_b = _redis_store()
    overlapping = {"hit": 0}
    inside = {"n": 0}

    async def worker(store: RedisSessionStore, tag: str):
        async with store.lock("shared"):
            inside["n"] += 1
            if inside["n"] > 1:
                overlapping["hit"] += 1
            await asyncio.sleep(0.05)
            inside["n"] -= 1

    try:
        await asyncio.gather(*[worker(replica_a, "a") for _ in range(4)], *[worker(replica_b, "b") for _ in range(4)])
        assert overlapping["hit"] == 0, "跨副本临界区发生重叠：分布式锁未生效"
    finally:
        await replica_a.close()
        await replica_b.close()


@pytest.mark.asyncio
async def test_redis_lock_wait_timeout_reports_session_busy(redis_store):
    replica_a = _redis_store(lock_wait_seconds=0.2)
    replica_b = _redis_store(lock_wait_seconds=0.2)
    released = asyncio.Event()
    try:
        async def holder():
            async with replica_a.lock("busy"):
                await asyncio.wait_for(released.wait(), timeout=5)

        task = asyncio.create_task(holder())
        await asyncio.sleep(0.15)
        with pytest.raises(ApiError) as excinfo:
            async with replica_b.lock("busy"):
                pass
        assert excinfo.value.code == "SESSION_BUSY" and excinfo.value.status_code == 409
        released.set()
        await task
        # 持有者释放后，其他副本应能立即获得锁
        async with replica_b.lock("busy"):
            pass
    finally:
        released.set()
        await replica_a.close()
        await replica_b.close()


@pytest.mark.asyncio
async def test_redis_lock_release_does_not_steal_successor_lock(redis_store):
    """锁过期被他人接管后，旧持有者的释放不得删除新持有者的锁（token 校验）。"""
    replica_a = _redis_store(lock_wait_seconds=0.1)
    replica_b = _redis_store(lock_wait_seconds=0.1)
    lock = replica_a.lock("steal")
    try:
        await lock.__aenter__()
        await replica_a._redis.delete(replica_a._lock_key("steal"))  # 模拟 A 的锁已过期
        async with replica_b.lock("steal"):
            await lock.__aexit__(None, None, None)  # A 此时才释放
            assert await replica_b._redis.get(replica_b._lock_key("steal")) is not None, "A 误删了 B 的锁"
    finally:
        await replica_a._redis.delete(replica_a._lock_key("steal"))
        await replica_a.close()
        await replica_b.close()


@pytest.mark.asyncio
async def test_redis_size_ignores_auxiliary_keys(redis_store):
    await redis_store.set("s1", [{"role": "user", "content": "1"}])
    await redis_store.set_pending("s1", [{"actionId": "a1"}])
    async with redis_store.lock("s1"):
        assert await redis_store.size() == 1, "待确认动作与锁键不应计入会话数"


@pytest.mark.asyncio
async def test_redis_lock_lease_is_renewed(redis_store):
    """租约续期：TTL 很小时，长耗时临界区仍应保持互斥（否则会出现锁中途失效）。"""
    replica_a = _redis_store(lock_ttl_seconds=1.2)
    replica_b = _redis_store(lock_wait_seconds=0.2)
    try:
        async with replica_a.lock("lease"):
            await asyncio.sleep(2.0)
            assert await replica_a._redis.get(replica_a._lock_key("lease")) is not None, "持有期间应持续续租"
            # 续租有效时，其他副本在此期间拿不到锁（等待上限内直接失败）
            with pytest.raises(ApiError):
                async with replica_b.lock("lease"):
                    pass
    finally:
        await replica_a.close()
        await replica_b.close()


@pytest.mark.asyncio
async def test_redis_unavailable_maps_to_explicit_error():
    """Redis 不可达时应给出明确错误码，而不是把驱动异常泄漏为 500。"""
    pytest.importorskip("redis")
    store = RedisSessionStore("redis://127.0.0.1:6399/0", ttl_seconds=60, prefix=TEST_PREFIX, lock_wait_seconds=0.1)
    try:
        with pytest.raises(ApiError) as excinfo:
            await asyncio.wait_for(store.get("s1"), timeout=5)
        assert excinfo.value.code == "SESSION_STORE_UNAVAILABLE" and excinfo.value.status_code == 503
        with pytest.raises(ApiError) as lock_exc:
            async with store.lock("s1"):
                pass
        assert lock_exc.value.code == "SESSION_STORE_UNAVAILABLE"
    finally:
        await store.close()


def test_build_session_store_requires_redis_url():
    with pytest.raises(RuntimeError, match="REDIS_URL"):
        build_session_store(driver="redis", max_sessions=10, ttl_seconds=60, redis_url="")
    store = build_session_store(driver="memory", max_sessions=10, ttl_seconds=60, redis_url="")
    assert isinstance(store, InMemorySessionStore)

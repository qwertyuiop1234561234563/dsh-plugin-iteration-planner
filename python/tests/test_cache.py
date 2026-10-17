"""缓存后端与 Redis 全局限流用例。

覆盖企业级「用中间件优化体验」的两个关键承诺：

- **缓存是纯优化**：只缓存确定性结果（向量化）、命中即省上游往返；后端故障一律
  fail-open（降级为未命中 / 放行），绝不因缓存抖动影响问答可用性；
- **限流是全局配额**：``REDIS_URL`` 下多个进程 / 副本共享同一令牌桶，
  实际全局速率 = 配置值（而不是「配置值 × 副本数」）。

Redis 相关用例在无可用实例时自动跳过（``TEST_REDIS_URL`` 可覆盖默认地址）。
"""

from __future__ import annotations

import asyncio
import os
from importlib.util import find_spec

import pytest

from app.cache import InMemoryCache, RedisCache, build_cache
from app.kb.embedder import CachedEmbedder
from app.security import MemoryRateLimitBackend, RedisRateLimiter, build_rate_limiter

REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:6379/15")
TEST_PREFIX = "iteration_planner:testcache:"
LIMIT_PREFIX = "iteration_planner:testlimit:"


# ------------------------------ 测试替身 ------------------------------


class CountingEmbedder:
    """记录调用次数的向量化替身（确定性伪向量，便于断言缓存命中）。"""

    def __init__(self, dimensions: int = 4, *, calls: int = 0, fail: bool = False) -> None:
        self._dimensions = dimensions
        self.calls = calls
        self.batches: list[list[str]] = []
        self._fail = fail

    @property
    def name(self) -> str:
        return "stub-embedding"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts):
        self.calls += 1
        self.batches.append(list(texts))
        if self._fail:
            raise RuntimeError("stub embedding failure")
        return [[float(len(t) % 7)] * self._dimensions for t in texts]

    async def aclose(self) -> None:
        return None


async def _redis_cache() -> RedisCache:
    pytest.importorskip("redis")
    cache = RedisCache(REDIS_URL, prefix=TEST_PREFIX)
    try:
        await cache._redis.ping()
    except Exception:  # pragma: no cover - 取决于本机是否有 Redis
        await cache.close()
        pytest.skip("本机无可用 Redis，跳过 Redis 用例")
    await _cleanup(cache)
    return cache


async def _cleanup(cache: RedisCache) -> None:
    async for key in cache._redis.scan_iter(match=f"{TEST_PREFIX}*", count=100):
        await cache._redis.delete(key)


# ------------------------------ 进程内存缓存 ------------------------------


@pytest.mark.asyncio
async def test_memory_cache_roundtrip_and_stats():
    cache = InMemoryCache(max_entries=8)
    assert await cache.get("missing") is None
    await cache.set("k", {"v": [1, 2]}, ttl_seconds=30)
    assert await cache.get("k") == {"v": [1, 2]}
    assert await cache.get_many(["k", "missing"]) == [{"v": [1, 2]}, None]
    description = cache.describe()
    assert description["backend"] == "memory"
    assert description["hits"] == 2 and description["misses"] == 2
    assert description["hitRate"] == 0.5
    await cache.delete("k")
    assert await cache.get("k") is None


@pytest.mark.asyncio
async def test_memory_cache_expires_and_evicts():
    cache = InMemoryCache(max_entries=2)
    await cache.set("short", "v", ttl_seconds=0.05)
    await asyncio.sleep(0.08)
    assert await cache.get("short") is None, "TTL 过期后应视为未命中"

    for name in ("a", "b", "c"):
        await cache.set(name, name, ttl_seconds=30)
    assert await cache.get("a") is None, "超出上限应淘汰最旧条目"
    assert await cache.get("c") == "c"
    assert cache.describe()["evictions"] == 1

    await cache.set("disabled", "v", ttl_seconds=0)
    assert await cache.get("disabled") is None, "ttl<=0 表示不缓存"


# ------------------------------ Redis 缓存 ------------------------------


@pytest.mark.asyncio
async def test_redis_cache_shares_and_expires():
    cache = await _redis_cache()
    other = RedisCache(REDIS_URL, prefix=TEST_PREFIX)
    try:
        await cache.set("shared", [0.1, 0.2], ttl_seconds=30)
        assert await other.get("shared") == [0.1, 0.2], "两个实例应共享同一份缓存"
        assert await other.get_many(["shared", "absent"]) == [[0.1, 0.2], None]
        await other.delete("shared")
        assert await cache.get("shared") is None, "删除应对所有副本可见"

        await cache.set("brief", "v", ttl_seconds=1)
        await asyncio.sleep(1.2)
        assert await cache.get("brief") is None, "TTL 到期后应视为未命中"
    finally:
        await _cleanup(cache)
        await cache.close()
        await other.close()


@pytest.mark.asyncio
async def test_redis_cache_fail_open_and_dirty_value():
    cache = await _redis_cache()
    try:
        # 脏数据（非 JSON）：按未命中处理，交由调用方重算覆盖，而不是抛错
        await cache._redis.set(f"{TEST_PREFIX}dirty", "not-json")
        assert await cache.get("dirty") is None
        assert cache.describe()["errors"] == 0, "脏数据不算后端故障"
    finally:
        await _cleanup(cache)
        await cache.close()

    broken = RedisCache("redis://127.0.0.1:6399/0", socket_timeout=0.2, connect_timeout=0.2)
    try:
        assert await broken.get("anything") is None, "后端不可达必须降级为未命中而非抛错"
        await broken.set("anything", 1, ttl_seconds=10)  # 不应抛错
        assert broken.describe()["errors"] >= 1, "故障应被计数以便告警"
    finally:
        await broken.close()


# ------------------------------ 向量化缓存包装 ------------------------------


@pytest.mark.asyncio
async def test_cached_embedder_hits_and_preserves_order():
    inner = CountingEmbedder()
    cache = InMemoryCache()
    embedder = CachedEmbedder(inner, cache, ttl_seconds=30)

    first = await embedder.embed(["a", "bb", "a"])
    assert inner.calls == 1, "首次应请求上游一次"
    assert inner.batches[0] == ["a", "bb"], "同批重复文本应去重后上行"
    assert first[0] == first[2], "重复输入应返回同一向量"
    assert len(first) == 3

    second = await embedder.embed(["bb", "a"])
    assert inner.calls == 1, "全部命中缓存时不应再请求上游"
    assert second == [first[1], first[0]], "缓存命中须保持与输入同序"
    assert embedder.dimensions == 4 and embedder.name.endswith("+cache")


@pytest.mark.asyncio
async def test_cached_embedder_coalesces_inflight_and_validates():
    inner = CountingEmbedder()
    cache = InMemoryCache()
    embedder = CachedEmbedder(inner, cache, ttl_seconds=30)

    results = await asyncio.gather(*(embedder.embed(["same"]) for _ in range(5)))
    assert inner.calls == 1, "并发相同批次应合并为一次上游调用"
    assert all(item == results[0] for item in results)

    # 维度不符的缓存值应视为未命中并重算（防止切换模型维度后拿到错向量）
    await cache.set(embedder._digest("same"), [0.0, 0.0], ttl_seconds=30)
    await embedder.embed(["same"])
    assert inner.calls == 2, "维度不符应重新向量化"

    empty = await embedder.embed([])
    assert empty == [] and inner.calls == 2, "空输入不应触发上游调用"


@pytest.mark.asyncio
async def test_cached_embedder_fail_open_cache_but_surfaces_upstream_error():
    inner = CountingEmbedder()
    cache = InMemoryCache()
    embedder = CachedEmbedder(inner, cache, ttl_seconds=30)

    # 缓存后端不可用时仍应正常返回（fail-open），不吞掉上游自身的故障。
    # ``redis`` 是可选依赖（见 requirements-redis.txt）：本机未安装时只跳过这一段，
    # 下面「上游故障必须原样暴露」的断言语义与缓存实现无关，照常执行。
    if find_spec("redis") is not None:
        broken = RedisCache("redis://127.0.0.1:6399/0", socket_timeout=0.2, connect_timeout=0.2)
        try:
            tolerant = CachedEmbedder(inner, broken, ttl_seconds=30)
            vectors = await tolerant.embed(["hello"])
            assert len(vectors) == 1 and len(vectors[0]) == 4, "缓存不可用不应影响向量化结果"
        finally:
            await broken.close()

    failing = CachedEmbedder(CountingEmbedder(fail=True), cache, ttl_seconds=30)
    with pytest.raises(RuntimeError):
        await failing.embed(["boom"])


# ------------------------------ 限流后端 ------------------------------


@pytest.mark.asyncio
async def test_memory_limiter_and_disabled():
    limiter = MemoryRateLimitBackend(per_minute=2, burst=2)
    assert limiter.name == "memory" and limiter.enabled
    outcomes = [await limiter.allow("ip-1") for _ in range(3)]
    assert [allowed for allowed, _ in outcomes] == [True, True, False]
    assert outcomes[2][1] >= 1, "被拒时应给出建议重试秒数"
    assert (await limiter.allow("ip-2"))[0] is True, "不同 key 应独立分桶"

    off = MemoryRateLimitBackend(per_minute=0)
    assert off.enabled is False
    assert (await off.allow("any"))[0] is True


@pytest.mark.asyncio
async def test_redis_limiter_shares_quota_across_instances():
    pytest.importorskip("redis")
    from redis.asyncio import Redis

    clients = [Redis.from_url(REDIS_URL, decode_responses=True) for _ in range(2)]
    limiters = [
        RedisRateLimiter(client, per_minute=4, burst=4, prefix=LIMIT_PREFIX) for client in clients
    ]
    try:
        await clients[0].ping()
    except Exception:  # pragma: no cover - 取决于本机是否有 Redis
        for client in clients:
            await client.aclose()
        pytest.skip("本机无可用 Redis，跳过全局限流用例")
    async for key in clients[0].scan_iter(match=f"{LIMIT_PREFIX}*", count=100):
        await clients[0].delete(key)
    try:
        # 两个「副本」交替请求同一 key：配额必须是**共享**的（合计 4 次）
        outcomes = []
        for index in range(6):
            outcomes.append(await limiters[index % 2].allow("shared-ip"))
        assert [allowed for allowed, _ in outcomes] == [True, True, True, True, False, False], (
            "跨副本应共享同一令牌桶，否则全局配额会被副本数放大"
        )
        denied_retry = outcomes[4][1]
        assert denied_retry >= 1
        assert (await limiters[1].allow("another-ip"))[0] is True, "不同 key 独立分桶"

        keys = [key async for key in clients[0].scan_iter(match=f"{LIMIT_PREFIX}*", count=100)]
        assert len(keys) == 2, "每个活跃 key 只应产生一个桶（并带 TTL，不无界增长）"
        ttl = await clients[0].pttl(keys[0])
        assert ttl > 0, "桶键必须带 TTL，避免空闲客户端产生无界键增长"
    finally:
        async for key in clients[0].scan_iter(match=f"{LIMIT_PREFIX}*", count=100):
            await clients[0].delete(key)
        for client in clients:
            await client.aclose()


@pytest.mark.asyncio
async def test_redis_limiter_fail_open_and_disabled():
    pytest.importorskip("redis")
    from redis.asyncio import Redis

    client = Redis.from_url("redis://127.0.0.1:6399/0", socket_timeout=0.2, socket_connect_timeout=0.2)
    limiter = RedisRateLimiter(client, per_minute=1, burst=1, prefix=LIMIT_PREFIX)
    try:
        allowed, retry_after = await limiter.allow("ip")
        assert allowed is True and retry_after == 0, "限流后端故障应 fail-open，不能拒绝正常用户"
    finally:
        await client.aclose()

    off = RedisRateLimiter(None, per_minute=0)
    assert off.enabled is False
    assert (await off.allow("ip"))[0] is True, "关闭限流时短路，不应触碰 Redis"


@pytest.mark.asyncio
async def test_limiter_close_is_safe_for_all_backends():
    """限流后端必须都能被安全释放：内存后端空实现，Redis 后端关闭连接池。"""
    await MemoryRateLimitBackend(per_minute=5).close()
    await RedisRateLimiter(None, per_minute=0).close()
    disabled = RedisRateLimiter(None, per_minute=0)
    await disabled.close()
    assert (await disabled.allow("ip"))[0] is True, "释放后关闭态的限流仍应短路放行"


# ------------------------------ 构建函数（显式选择时不静默降级） ------------------------------


def test_build_cache_and_limiter_selection():
    assert build_cache(driver="memory").name == "memory"
    assert build_cache(driver="auto").name == "memory", "未配置 REDIS_URL 时 auto 应为内存后端"
    assert build_rate_limiter(per_minute=10).name == "memory"

    with pytest.raises(RuntimeError, match="REDIS_URL"):
        build_cache(driver="redis", redis_url="")
    with pytest.raises(RuntimeError, match="REDIS_URL"):
        build_rate_limiter(per_minute=10, driver="redis", redis_url="")

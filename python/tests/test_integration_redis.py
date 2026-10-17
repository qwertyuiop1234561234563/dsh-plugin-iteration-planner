"""跨副本共享后端的端到端回归（把「Redis / 中间件优化体验」的验证固化进测试套件）。

此前这套场景以一次性临时脚本的形式跑过（16/16 通过），但脚本用完即弃 —— 一旦
``CachedEmbedder`` / ``RedisRateLimiter`` / 应用装配发生回归，没有任何门禁能拦住。
本文件用 ``TestClient`` 驱动**真实应用实例**（两个独立 app = 两个「副本」），
把以下承诺固化为 CI 持续守护的断言：

- **E1** 一轮对话内「记忆召回 + 知识库检索」对同一段输入只向量化一次（同批去重 / 命中缓存）；
- **E2** 重复提问直接命中缓存，零上游往返；
- **E3** ``/api/health`` 与 ``/metrics`` 透出缓存后端、命中率与命中计数；
- **E4** 副本 B 复用副本 A 写入的 Redis 缓存（跨副本共享）；
- **E5** Redis 限流在副本间共享**同一**配额（合计放行 = 配置值）；
- **E6** 对照：内存限流下两副本各自持一份配额（故多副本必须用 Redis）；
- **E7** Redis 不可达时缓存与限流**一律 fail-open**，不影响问答可用性，且故障被计数。

无可用 Redis 时相关用例自动跳过（``TEST_REDIS_URL`` 可覆盖默认地址）；
E7 不需要真实 Redis（指向一个不可达端口即可）。
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.constants import KB_COLLECTION, MEMORY_COLLECTION
from app.engine_client import EngineClient
from app.kb import ChromaVectorStore
from app.main import create_app
from tests.conftest import ScriptedLlm, StubEmbedder, _engine_handler, auth

REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:6379/15")
UNREACHABLE_REDIS_URL = "redis://127.0.0.1:6399/0"
#: 与 cache.py / security.py 的默认前缀一致（清理用，避免历史残留干扰配额断言）
CACHE_PREFIX = "iteration_planner:cache:*"
LIMIT_PREFIX = "iteration_planner:ratelimit:*"

KB_DOC: dict[str, Any] = {
    "title": "排期规范",
    "content": "关键路径必须由 AOE 算法计算，不允许手工排期。灰度发布先小流量验证再逐步放量。",
}
MEMORY_TEXT = "关键路径怎么算都按 AOE 结果"
ANSWER = "依据规范：关键路径由 AOE 算法计算。"


# ------------------------------ 夹具与工具 ------------------------------


async def _require_redis() -> None:
    """无可用 Redis 时跳过（保持本地无依赖可跑，CI 里起真实实例真跑）。"""
    pytest.importorskip("redis")
    from redis.asyncio import Redis

    client = Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        await client.ping()
    except Exception:  # pragma: no cover - 取决于本机是否有 Redis
        await client.aclose()
        pytest.skip("本机无可用 Redis，跳过跨副本共享用例")
    await client.aclose()


async def _flush(*patterns: str) -> None:
    """清理指定前缀的键：让配额与命中率断言从干净状态出发。"""
    from redis.asyncio import Redis

    client = Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        for pattern in patterns:
            keys = [key async for key in client.scan_iter(match=pattern, count=200)]
            if keys:
                await client.delete(*keys)
    except Exception:  # pragma: no cover - 清理失败不应让用例失败
        pass
    finally:
        await client.aclose()


def _scripted(times: int = 1) -> ScriptedLlm:
    return ScriptedLlm([{"role": "assistant", "content": ANSWER} for _ in range(times)])


def _replica(
    tmp_path,
    name: str,
    embedder: StubEmbedder,
    *,
    cache_backend: str = "redis",
    rate_limit_backend: str = "memory",
    per_minute: int = 0,
    redis_url: str = REDIS_URL,
    llm: ScriptedLlm | None = None,
) -> TestClient:
    """构建一个「副本」：独立向量库 + 独立 app 实例（缓存 / 限流按参数走 Redis 或内存）。"""
    store = ChromaVectorStore(
        str(tmp_path / name / "chroma"),
        chunk_collection=KB_COLLECTION,
        memory_collection=MEMORY_COLLECTION,
    )
    settings = Settings(
        _env_file=None,  # 测试不读本机 .env，保证可复现
        api_token="secret-token",
        api_base_path="/api",
        engine_base_url="http://engine.test",
        llm_base_url="",
        llm_model="",
        web_static_dir="",
        # 会话固定走内存：本文件聚焦缓存 / 限流后端，避免会话存储的可用性问题混入
        session_store="memory",
        redis_url=redis_url,
        cache_backend=cache_backend,
        cache_ttl_seconds=300,
        rate_limit_backend=rate_limit_backend,
        api_rate_limit_per_minute=per_minute,
        api_rate_limit_burst=per_minute,
        embedding_base_url="http://stub.test/v1",
        embedding_api_key="stub-key",
        embedding_model="stub-embedding",
        memory_enabled=True,
        memory_extract=False,
        kb_hybrid=True,
        kb_auto_inject=True,
        kb_inject_top_k=3,
        memory_top_k=4,
    )
    app = create_app(
        settings,
        engine=EngineClient("http://engine.test", "/api", 5.0, transport=httpx.MockTransport(_engine_handler)),
        llm=llm,
        embedder=embedder,
        vector_store=store,
        start_bridge=False,
    )
    return TestClient(app)


def _seed(client: TestClient, headers: dict[str, str]) -> None:
    """播种知识库与长期记忆：库为空时检索会提前返回，无法验证缓存是否生效。"""
    kb = client.post("/api/kb/documents", json=KB_DOC, headers=headers)
    assert kb.is_success, f"知识库播种失败：{kb.status_code} {kb.text}"
    memory = client.post(
        "/api/memories",
        json={"text": MEMORY_TEXT, "type": "fact", "importance": "high", "tags": ["排期"]},
        headers=headers,
    )
    assert memory.is_success, f"长期记忆播种失败：{memory.status_code} {memory.text}"


def _python_block(client: TestClient) -> dict[str, Any]:
    payload = client.get("/api/health").json()
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    return data.get("python") or {}


# ------------------------------ E1 / E2 ------------------------------


async def test_embedding_is_cached_within_turn_and_across_requests(tmp_path):
    """一轮内去重 + 重复提问命中缓存：上游向量化次数必须收敛到 1 与 0。"""
    await _require_redis()
    await _flush(CACHE_PREFIX)
    embedder = StubEmbedder()

    with _replica(tmp_path, "replica-a", embedder, llm=_scripted(3)) as client:
        headers = auth()
        _seed(client, headers)

        before = len(embedder.calls)
        first = client.post("/api/agent/chat", json={"message": "关键路径怎么算？"}, headers=headers)
        assert first.is_success, first.text
        after_first = len(embedder.calls)
        # 记忆召回与知识库检索用同一段输入：未命中缓存时应为 2 次，命中缓存后收敛为 1 次
        assert after_first - before == 1, "同一轮内对同一段输入应只向量化一次"

        second = client.post("/api/agent/chat", json={"message": "关键路径怎么算？"}, headers=headers)
        assert second.is_success, second.text
        assert len(embedder.calls) == after_first, "重复提问应命中缓存，零上游往返"

        third = client.post("/api/agent/chat", json={"message": "灰度发布流程是怎样的？"}, headers=headers)
        assert third.is_success, third.text
        assert len(embedder.calls) == after_first + 1, "新输入应重新向量化一次（且仅一次）"

        block = _python_block(client)
        assert block["cache"]["backend"] == "redis"
        assert block["cache"]["hits"] >= 2 and block["cache"]["errors"] == 0, block["cache"]


# ------------------------------ E3 ------------------------------


async def test_health_and_metrics_expose_cache(tmp_path):
    """可观测：``/api/health`` 透出缓存后端与命中率，``/metrics`` 暴露缓存命中计数。"""
    await _require_redis()
    await _flush(CACHE_PREFIX)
    embedder = StubEmbedder()

    with _replica(tmp_path, "replica-a", embedder, llm=_scripted(2)) as client:
        headers = auth()
        _seed(client, headers)
        for _ in range(2):
            assert client.post("/api/agent/chat", json={"message": "关键路径怎么算？"}, headers=headers).is_success

        block = _python_block(client)
        cache = block["cache"]
        assert cache["backend"] == "redis"
        assert cache["hits"] >= 1 and cache["hitRate"] is not None
        assert block["rateLimitBackend"] == "memory"

        metrics = client.get("/metrics").text
        assert 'iteration_planner_cache_operations_total{backend="redis",outcome="hit"}' in metrics
        assert "# TYPE iteration_planner_cache_operations_total counter" in metrics


# ------------------------------ E4 ------------------------------


async def test_cache_is_shared_across_replicas(tmp_path):
    """副本 B 必须命中副本 A 写入的 Redis 缓存（否则多副本各算各的，缓存价值减半）。"""
    await _require_redis()
    await _flush(CACHE_PREFIX)
    embedder = StubEmbedder()  # 两个副本共用同一个「上游」桩，便于统计总调用次数

    with _replica(tmp_path, "replica-a", embedder, llm=_scripted(1)) as replica_a, _replica(
        tmp_path, "replica-b", embedder, llm=_scripted(1)
    ) as replica_b:
        _seed(replica_a, auth())
        _seed(replica_b, auth())

        assert replica_a.post("/api/agent/chat", json={"message": "关键路径怎么算？"}, headers=auth()).is_success
        before = len(embedder.calls)
        assert replica_b.post("/api/agent/chat", json={"message": "关键路径怎么算？"}, headers=auth()).is_success
        assert len(embedder.calls) == before, "副本 B 应直接复用副本 A 的缓存，不再向量化"


# ------------------------------ E5 / E6 ------------------------------


async def test_redis_quota_is_global_while_memory_is_per_replica(tmp_path):
    """Redis 限流跨副本共享同一配额；内存限流下两副本各持一份（故多副本必须用 Redis）。"""
    await _require_redis()
    await _flush(LIMIT_PREFIX)
    embedder = StubEmbedder()

    async def _alternate(rate_limit_backend: str) -> list[int]:
        with _replica(
            tmp_path,
            f"limit-{rate_limit_backend}-a",
            embedder,
            cache_backend="memory",
            rate_limit_backend=rate_limit_backend,
            per_minute=4,
        ) as replica_a, _replica(
            tmp_path,
            f"limit-{rate_limit_backend}-b",
            embedder,
            cache_backend="memory",
            rate_limit_backend=rate_limit_backend,
            per_minute=4,
        ) as replica_b:
            clients = [replica_a, replica_b]
            return [clients[index % 2].get("/api/plans", headers=auth()).status_code for index in range(8)]

    shared = await _alternate("redis")
    assert shared.count(200) == 4, f"Redis 全局配额应为 4（跨副本共享同一个令牌桶），实际 {shared}"
    assert shared.count(429) == 4, f"超限应返回 429，实际 {shared}"

    await _flush(LIMIT_PREFIX)
    independent = await _alternate("memory")
    assert independent.count(200) == 8, (
        f"内存限流在两副本下各放行 4 次（共 8 次）—— 这正是多副本必须改用 Redis 的原因，实际 {independent}"
    )


# ------------------------------ E7 ------------------------------


async def test_fail_open_when_redis_unreachable(tmp_path):
    """Redis 不可达：缓存降级为未命中、限流降级为放行，问答链路不受影响且故障被计数。"""
    embedder = StubEmbedder()
    with _replica(
        tmp_path,
        "replica-broken",
        embedder,
        cache_backend="redis",
        rate_limit_backend="redis",
        per_minute=2,
        redis_url=UNREACHABLE_REDIS_URL,
        llm=_scripted(1),
    ) as client:
        headers = auth()
        _seed(client, headers)

        assert client.post("/api/agent/chat", json={"message": "关键路径怎么算？"}, headers=headers).is_success
        codes = [client.get("/api/plans", headers=headers).status_code for _ in range(6)]
        assert codes == [200] * 6, f"限流后端故障必须 fail-open，不得误限正常用户，实际 {codes}"

        cache = _python_block(client)["cache"]
        assert cache["backend"] == "redis"
        assert cache["errors"] >= 1, "缓存后端故障必须被计数，否则「缓存静默失效」无法告警"
        assert cache["hits"] == 0, "后端不可达时不应计为命中"

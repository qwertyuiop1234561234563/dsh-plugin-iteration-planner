"""幂等键测试：回放、冲突、指纹不符、失败释放、降级 fail-open、多用户隔离。

对应用例按「客户端为什么需要它」组织：超时重试、并发双发、错误键复用、跨用户撞键。
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.idempotency import (
    DECISION_CONFLICT,
    DECISION_DEGRADED,
    DECISION_MISMATCH,
    DECISION_PROCEED,
    DECISION_REPLAY,
    Decision,
    IdempotencyGuard,
    build_idempotency_guard,
    fingerprint,
    scoped_key,
)
from app.kvstore import InMemoryKV, KvStoreError
from app.main import create_app

from .conftest import auth, login


class CountingEngine:
    """记录调用次数的引擎桩：幂等的唯一硬判据是「引擎被真正调用了几次」。"""

    def __init__(self, *, status_code: int = 201, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self.payload = payload if payload is not None else {"data": {"planId": "p-idem"}}
        self.calls = 0

    async def forward(self, method, path, *, params=None, content=None, headers=None, timeout=None):
        self.calls += 1
        return httpx.Response(self.status_code, json=self.payload)

    async def aclose(self) -> None:
        return None


class BrokenKV(InMemoryKV):
    """所有操作都失败的 KV：验证「幂等保护失效但仍不阻断主链路」。"""

    async def set_nx(self, key, value, ttl):
        raise KvStoreError("idempotency", "set_nx", RuntimeError("redis down"))

    async def get(self, key):
        raise KvStoreError("idempotency", "get", RuntimeError("redis down"))


@pytest.fixture
def opened(settings):
    stack = contextlib.ExitStack()

    def _open(engine, **overrides) -> TestClient:
        cfg = settings.model_copy(update=overrides or {})
        return stack.enter_context(TestClient(create_app(cfg, engine=engine, start_bridge=False)))

    yield _open
    stack.close()


def _post(client: TestClient, key: str | None, *, body=None, headers=None):
    merged = dict(headers or auth())
    if key is not None:
        merged["Idempotency-Key"] = key
    return client.post("/api/plans", json=body or {"requirement": "幂等建计划"}, headers=merged)


# ------------------------------ 纯函数 / 守卫 ------------------------------


def test_fingerprint_is_order_insensitive_for_query_params() -> None:
    a = fingerprint("POST", "/api/plans", [("b", "2"), ("a", "1")], b"{}")
    b = fingerprint("POST", "/api/plans", [("a", "1"), ("b", "2")], b"{}")
    assert a == b

    # 方法 / 路径 / 请求体任一变化都必须改指纹
    assert fingerprint("PUT", "/api/plans", [("a", "1")], b"{}") != a
    assert fingerprint("POST", "/api/plans/x", [("a", "1")], b"{}") != a
    assert fingerprint("POST", "/api/plans", [("a", "1")], b'{"x":1}') != a


@pytest.mark.asyncio
async def test_guard_decisions_cover_all_four_outcomes() -> None:
    guard = IdempotencyGuard(InMemoryKV(store="test", max_entries=16), ttl_seconds=60.0)
    key = scoped_key("u1", "k1")

    assert (await guard.decide(key, "fp1")).kind == DECISION_PROCEED  # 首次
    assert (await guard.decide(key, "fp1")).kind == DECISION_CONFLICT  # 处理中
    assert (await guard.decide(key, "fp2")).kind == DECISION_MISMATCH  # 异载荷

    await guard.finish(key, {"fingerprint": "fp1", "status": 201})
    replay = await guard.decide(key, "fp1")
    assert replay.kind == DECISION_REPLAY  # 已完成 → 回放
    assert replay.response["status"] == 201


@pytest.mark.asyncio
async def test_guard_fails_open_when_store_unavailable() -> None:
    guard = IdempotencyGuard(BrokenKV(store="test", max_entries=16), ttl_seconds=60.0)

    decision = await guard.decide(scoped_key("u1", "k1"), "fp1")

    # 保护措施本身不能变成新的可用性缺口
    assert decision.kind == DECISION_DEGRADED


def test_build_requires_redis_url_when_driver_is_redis() -> None:
    # 多副本下退化成「各副本各存各的键」等于幂等形同虚设，必须启动即失败
    with pytest.raises(RuntimeError):
        build_idempotency_guard(driver="redis", redis_url="")


# ------------------------------ 端到端：回放 ------------------------------


def test_replay_returns_same_response_without_reexecuting(settings, opened) -> None:
    engine = CountingEngine()
    client = opened(engine)

    first = _post(client, "retry-1")
    second = _post(client, "retry-1")

    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    # 键被重放，引擎只应真正执行一次
    assert engine.calls == 1
    assert first.headers.get("Idempotency-Replayed") is None
    assert second.headers["Idempotency-Replayed"] == "true"


def test_replay_preserves_location_of_async_job(settings, opened) -> None:
    """幂等 + 异步叠加：重试拿到的必须是**同一个作业**，而不是又建一个。"""
    engine = CountingEngine()
    client = opened(engine)
    headers = {**auth(), "Prefer": "respond-async"}

    first = _post(client, "retry-async", headers=headers)
    second = _post(client, "retry-async", headers=headers)

    assert first.status_code == second.status_code == 202
    assert first.headers["Location"] == second.headers["Location"]
    assert first.headers.get("Prefer") == "respond-async"
    # 回放时必须带上 Location：否则调用方拿不到作业地址
    assert second.headers["Location"] == first.headers["Location"]
    assert second.headers["Idempotency-Replayed"] == "true"


def test_different_key_executes_again(settings, opened) -> None:
    engine = CountingEngine()
    client = opened(engine)

    _post(client, "k-a")
    _post(client, "k-b")

    assert engine.calls == 2


def test_no_key_means_no_idempotency(settings, opened) -> None:
    engine = CountingEngine()
    client = opened(engine)

    _post(client, None)
    _post(client, None)

    # 未提供键的请求无从判断「是否同一意图」，只能各自执行
    assert engine.calls == 2


# ------------------------------ 端到端：冲突与拒绝 ------------------------------


def test_same_key_with_different_payload_is_rejected(settings, opened) -> None:
    engine = CountingEngine()
    client = opened(engine)
    _post(client, "k-mismatch")

    conflict = _post(client, "k-mismatch", body={"requirement": "换了需求"})

    assert conflict.status_code == 422
    assert conflict.json()["code"] == "IDEMPOTENCY_KEY_REUSE"
    assert engine.calls == 1  # 异载荷请求绝不能被放行执行


class ConflictGuard:
    """恒返回「同键请求仍在处理中」的守卫。

    为什么用桩而不是并发双发：在进程内用 ``TestClient`` 复现「真并发」需要跨事件循环，
    测出来的往往是测试框架的调度行为而非产品语义。in-flight 判定本身已由
    ``test_guard_decisions_cover_all_four_outcomes`` 直接验证；这里只确定性地验证
    「CONFLICT 会被映射成 409 + Retry-After 下发到客户端」。
    """

    backend = "memory"

    async def decide(self, key, request_fingerprint) -> Decision:
        return Decision(DECISION_CONFLICT)

    async def finish(self, key, response) -> None:
        return None

    async def release(self, key) -> None:
        return None

    async def close(self) -> None:
        return None


def test_in_flight_duplicate_returns_409_with_retry_after(settings, opened) -> None:
    client = opened(CountingEngine())
    client.app.state.ctx.idempotency = ConflictGuard()

    response = _post(client, "k-concurrent")

    assert response.status_code == 409
    assert response.json()["code"] == "IDEMPOTENCY_IN_PROGRESS"
    assert response.headers["Retry-After"] == "1"


def test_oversized_key_is_rejected(settings, opened) -> None:
    engine = CountingEngine()
    client = opened(engine)

    response = _post(client, "x" * 256)

    assert response.status_code == 400
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert engine.calls == 0


# ------------------------------ 端到端：失败与降级 ------------------------------


def test_failed_request_releases_key_for_retry(settings, opened) -> None:
    engine = CountingEngine(status_code=409, payload={"code": "CYCLE_DEPENDENCY", "message": "存在循环依赖"})
    client = opened(engine)

    first = _post(client, "k-fail")
    second = _post(client, "k-fail")

    # 失败响应没有回放价值：释放占位后同一个键可以重试
    assert first.status_code == second.status_code == 409
    assert first.json() == second.json()
    assert engine.calls == 2


def test_degraded_store_does_not_block_and_is_flagged(settings, opened) -> None:
    engine = CountingEngine()
    client = opened(engine)
    ctx = client.app.state.ctx
    ctx.idempotency = IdempotencyGuard(BrokenKV(store="test", max_entries=16), ttl_seconds=60.0)

    first = _post(client, "k-degraded")
    second = _post(client, "k-degraded")

    # 存储坏了也必须放行：保护措施故障不能升级成服务不可用
    assert first.status_code == second.status_code == 201
    assert first.headers["Idempotency-Status"] == "skipped"
    assert engine.calls == 2


# ------------------------------ 隔离与作用域 ------------------------------


def test_keys_are_isolated_by_principal(settings, opened) -> None:
    engine = CountingEngine()
    client = opened(engine)

    machine = _post(client, "shared-key")  # 机器令牌主体
    login(client)  # 换成人类用户主体：Cookie 生效后不再附带机器令牌
    human = client.post("/api/plans", json={"requirement": "幂等建计划"}, headers={"Idempotency-Key": "shared-key"})

    assert human.status_code == 201
    # 两个主体碰巧用了同一个键，不能互相串写
    assert engine.calls == 2
    assert human.headers.get("Idempotency-Status") is None


def test_built_in_guard_audits_backend(settings) -> None:
    client = TestClient(create_app(settings, engine=CountingEngine(), start_bridge=False))
    guard = client.app.state.ctx.idempotency
    assert guard is not None
    described = guard.describe()
    assert described["ttlSeconds"] == settings.idempotency_ttl_seconds
    assert described["backend"] in ("memory", "redis")


def test_reads_are_not_idempotency_guarded(settings, opened) -> None:
    calls: list[str] = []

    class ReadEngine(CountingEngine):
        async def forward(self, method, path, **kwargs):
            calls.append(f"{method} {path}")
            return httpx.Response(200, json={"data": {"items": []}})

    client = opened(ReadEngine())
    response = client.get("/api/plans", headers={**auth(), "Idempotency-Key": "read-key"})

    assert response.status_code == 200
    assert "Idempotency-Status" not in response.headers
    assert "Idempotency-Replayed" not in response.headers
    assert calls == ["GET /api/plans"]

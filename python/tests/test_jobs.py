"""异步作业链路测试：202 受理、快照轮询、SSE 进度、取消、属主隔离。

全程使用桩引擎（无外部依赖）：快引擎验证「受理 → 执行 → 成功」全链路，
慢引擎（可控延时）用于观察中间态与取消语义。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app

from .conftest import auth, login


class StubEngine:
    """最小引擎桩：``forward`` 可控速，可直接观察作业中间态。

    签名与 :meth:`app.engine_client.EngineClient.forward` 对齐（含 ``timeout``），
    因此还能顺带断言「作业路径使用的是独立的作业超时预算」。
    """

    def __init__(
        self,
        *,
        delay: float = 0.0,
        status_code: int = 201,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.delay = delay
        self.status_code = status_code
        self.payload = payload if payload is not None else {"data": {"planId": "p-async"}}
        self.calls: list[dict[str, Any]] = []

    async def forward(self, method, path, *, params=None, content=None, headers=None, timeout=None):
        self.calls.append({"method": method, "path": path, "timeout": timeout})
        if self.delay:
            await asyncio.sleep(self.delay)
        return httpx.Response(self.status_code, json=self.payload)

    async def aclose(self) -> None:
        return None


@pytest.fixture
def opened(settings):
    """构建**进入 lifespan** 的客户端。

    为什么必须进入上下文：``TestClient`` 不进入上下文时，每个请求各自新建并销毁事件循环，
    在请求返回时把尚未跑完的后台任务当成「遗留任务」取消掉——那会把「作业执行中」直接
    变成「作业已取消」，测出来的全是假象。进入上下文后事件循环在用例期间持续存活，
    后台作业才能像生产环境（uvicorn）那样推进。
    """

    stack = contextlib.ExitStack()

    def _open(engine, **overrides) -> TestClient:
        cfg = settings.model_copy(update=overrides or {})
        return stack.enter_context(TestClient(create_app(cfg, engine=engine, start_bridge=False)))

    yield _open
    stack.close()


def _wait_for(client: TestClient, url: str, statuses: set[str], *, headers: dict[str, str], timeout: float = 10.0):
    """轮询作业直到进入目标状态（后台任务与请求在同一个事件循环内推进）。"""
    deadline = time.monotonic() + timeout
    snapshot: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        response = client.get(url, headers=headers)
        assert response.status_code == 200, response.text
        snapshot = response.json()["data"]
        if snapshot["status"] in statuses:
            return snapshot
        time.sleep(0.05)
    raise AssertionError(f"作业未在 {timeout}s 内进入 {statuses}：{snapshot}")


def _submit(client: TestClient, path: str = "/api/plans", *, body: dict[str, Any] | None = None):
    headers = {**auth(), "Prefer": "respond-async"}
    response = client.post(path, json=body or {"requirement": "异步建计划"}, headers=headers)
    assert response.status_code == 202, response.text
    return response


# ---------------------------------- 受理 ----------------------------------


def test_async_plan_returns_202_with_job_resource(settings, opened) -> None:
    engine = StubEngine()
    client = opened(engine)

    accepted = _submit(client)
    data = accepted.json()["data"]
    job_id = data["jobId"]

    assert data["kind"] == "plan.create"
    assert data["status"] in ("pending", "running")
    # 作业资源地址必须可直达：Location 与 pollUrl 一致，streamUrl 为其子路径
    assert accepted.headers["Location"] == f"/api/jobs/{job_id}"
    assert data["pollUrl"] == f"/api/jobs/{job_id}"
    assert data["streamUrl"] == f"/api/jobs/{job_id}/stream"
    # 回显 Prefer，调用方无需自行推断本次是按同步还是异步语义受理
    assert accepted.headers["Prefer"] == "respond-async"

    snapshot = _wait_for(client, data["pollUrl"], {"succeeded"}, headers=auth())
    assert snapshot["planId"] == "p-async"
    assert snapshot["result"]["statusCode"] == 201
    assert [p["phase"] for p in snapshot["progress"]] == ["queued", "engine", "settled"]
    # 作业路径用独立的作业超时预算，而不是普通接口的短超时
    assert engine.calls[0]["path"] == "/api/plans"
    assert engine.calls[0]["timeout"] == settings.engine_job_timeout_seconds


def test_prefer_header_with_multiple_preferences_still_async(settings, opened) -> None:
    client = opened(StubEngine())
    headers = {**auth(), "Prefer": "return=minimal, respond-async"}
    response = client.post("/api/plans", json={"requirement": "x"}, headers=headers)
    assert response.status_code == 202, response.text


def test_without_prefer_header_write_stays_synchronous(settings, opened) -> None:
    engine = StubEngine(status_code=201)
    client = opened(engine)

    response = client.post("/api/plans", json={"requirement": "同步建计划"}, headers=auth())

    assert response.status_code == 201
    assert response.json()["data"]["planId"] == "p-async"
    # 同步长耗时写放宽到「引擎真正的预算」，避免短超时造成的「报失败但已落库」
    assert engine.calls[0]["timeout"] == settings.engine_long_timeout_seconds


def test_non_job_path_ignores_prefer_header(settings, opened) -> None:
    engine = StubEngine(status_code=200, payload={"data": {"hasCycle": False}})
    client = opened(engine)
    headers = {**auth(), "Prefer": "respond-async"}

    response = client.post("/api/plans/p1/dag-check", json={}, headers=headers)

    # 快接口不应被作业化：否则调用方凭空多两次往返
    assert response.status_code == 200
    assert engine.calls[0]["timeout"] is None


# ---------------------------------- 查询 ----------------------------------


def test_unknown_and_malformed_job_ids_both_return_404(settings, opened) -> None:
    client = opened(StubEngine())
    unknown = client.get(f"/api/jobs/{'a' * 32}", headers=auth())
    malformed = client.get("/api/jobs/not-a-job-id", headers=auth())
    cancel = client.post("/api/jobs/not-a-job-id/cancel", headers=auth())

    for response in (unknown, malformed, cancel):
        assert response.status_code == 404, response.text
        assert response.json()["code"] == "JOB_NOT_FOUND"


# ---------------------------------- 取消 ----------------------------------


def test_cancel_marks_job_terminal_and_rejects_second_cancel(settings, opened) -> None:
    engine = StubEngine(delay=5.0)
    client = opened(engine)
    job_url = _submit(client).json()["data"]["pollUrl"]

    cancelled = client.post(f"{job_url}/cancel", headers=auth())
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["data"]["status"] == "cancelled"

    snapshot = client.get(job_url, headers=auth()).json()["data"]
    assert snapshot["status"] == "cancelled"
    assert snapshot["progress"][-1]["message"] == "作业已取消"

    # 终态作业不可重复操作：返回冲突而不是「假装又取消了一次」
    again = client.post(f"{job_url}/cancel", headers=auth())
    assert again.status_code == 409
    assert again.json()["code"] == "JOB_CONFLICT"


def test_cancel_after_engine_completed_keeps_real_result(settings, opened) -> None:
    """取消无法中断引擎：引擎已完成时必须如实记录结果，而不是假装「未执行」。"""
    engine = StubEngine(delay=0.4)
    client = opened(engine)
    accepted = _submit(client).json()["data"]

    # 等引擎已经算完（作业进入终态），此时再取消必然撞上 409，说明状态是真实的
    snapshot = _wait_for(client, accepted["pollUrl"], {"succeeded"}, headers=auth())
    assert snapshot["status"] == "succeeded"

    conflict = client.post(f"{accepted['pollUrl']}/cancel", headers=auth())
    assert conflict.status_code == 409


# ---------------------------------- 属主隔离 ----------------------------------


def test_jobs_are_isolated_by_owner(settings, opened) -> None:
    engine = StubEngine(delay=0.5)
    client = opened(engine)
    accepted = _submit(client).json()["data"]
    job_url = accepted["pollUrl"]

    # 切换到人类用户（引导管理员）：Cookie 生效后主体不再是机器令牌
    login(client)
    hidden = client.get(job_url)
    hidden_cancel = client.post(f"{job_url}/cancel")

    # 非属主一律 404：否则可借状态码差异探测「别人是否存在这个作业」
    assert hidden.status_code == 404, hidden.text
    assert hidden.json()["code"] == "JOB_NOT_FOUND"
    assert hidden_cancel.status_code == 404


# ---------------------------------- 进度流 ----------------------------------


def test_progress_stream_emits_progress_then_done(settings, opened) -> None:
    client = opened(StubEngine())
    stream_url = _submit(client).json()["data"]["streamUrl"]

    response = client.get(stream_url, headers=auth())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: progress" in response.text
    assert "event: done" in response.text
    assert '"planId": "p-async"' in response.text


def test_progress_stream_reports_failure_as_error_event(settings, opened) -> None:
    engine = StubEngine(status_code=409, payload={"code": "CYCLE_DEPENDENCY", "message": "存在循环依赖"})
    client = opened(engine)
    accepted = _submit(client).json()["data"]

    snapshot = _wait_for(client, accepted["pollUrl"], {"failed"}, headers=auth())
    assert snapshot["error"]["code"] == "CYCLE_DEPENDENCY"

    stream = client.get(accepted["streamUrl"], headers=auth())
    assert "event: error" in stream.text
    assert "event: done" not in stream.text


def test_job_failure_maps_engine_unreachable(settings, opened) -> None:
    from app.engine_client import EngineClient

    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("engine down", request=request)

    # 走真实客户端：验证「传输层故障 → 502 语义 → 作业只落稳定错误码」这条链路
    engine = EngineClient("http://engine.test", "/api", 5.0, transport=httpx.MockTransport(_boom))
    client = opened(engine)
    accepted = _submit(client).json()["data"]

    snapshot = _wait_for(client, accepted["pollUrl"], {"failed"}, headers=auth(), timeout=20.0)

    # 作业失败只落稳定错误码与通用文案，绝不把上游连接细节带出
    assert snapshot["error"]["code"] == "ENGINE_UNAVAILABLE"
    assert "engine down" not in snapshot["error"]["message"]


def test_sync_long_running_write_timeout_returns_504_with_proxy_status(settings, opened) -> None:
    """同步长耗时写读超时 → 504 `http_response_timeout`，而**不是** 502「不可达」。

    这两者的运维含义完全不同（RFC 9110 + IANA Proxy-Status）：超时说明**引擎可达但太慢**，
    响应可能已经在路上；「不可达」说明根本没连上。把超时报成不可达会诱导运维去查网络，
    也会让调用方误以为重试安全（实际上游仍在跑，重试会再落一份数据）。
    """
    from app.engine_client import EngineClient

    def _slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timeout", request=request)

    # 走真实客户端：验证「读超时 → 504 语义 → Proxy-Status 首部」这条链路
    engine = EngineClient("http://engine.test", "/api", 5.0, transport=httpx.MockTransport(_slow))
    client = opened(engine)
    # 不带 Prefer：走同步路径（这正是实测中报 502 却已落库的那条路）
    response = client.post("/api/plans", json={"requirement": "订单中心"}, headers=auth())

    assert response.status_code == 504
    assert response.json()["code"] == "ENGINE_TIMEOUT"
    assert response.headers["Proxy-Status"] == (
        'iteration_planner; error="http_response_timeout"'
    )
    # 5xx 不得泄漏上游地址等内部细节
    assert "engine.test" not in response.text

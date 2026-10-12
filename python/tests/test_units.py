"""单元测试：WS 地址推导、错误信封、引擎客户端解包/映射、工具调度映射。"""

from __future__ import annotations

import httpx
import pytest

from app.agent.tools import ToolDispatcher
from app.engine_client import EngineClient
from app.errors import ApiError, bad_request, unauthorized
from app.main import derive_engine_ws_url


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("http://engine.test:8080", "ws://engine.test:8080/ws"),
        ("https://engine.test", "wss://engine.test/ws"),
    ],
)
def test_derive_engine_ws_url(base, expected):
    assert derive_engine_ws_url(base, "/ws") == expected


def test_error_body_shape():
    assert unauthorized().to_body() == {"code": "UNAUTHORIZED", "message": "缺少或无效的访问令牌"}
    body = bad_request("入参错误", [{"field": "requirement", "message": "不能为空"}]).to_body()
    assert body["code"] == "VALIDATION_ERROR"
    assert body["details"][0]["field"] == "requirement"


async def test_engine_client_unwraps_envelope():
    def handler(_request):
        return httpx.Response(200, json={"data": {"ok": True}})

    client = EngineClient("http://e.test", "/api", 5.0, transport=httpx.MockTransport(handler))
    assert await client.request("GET", "/plans") == {"ok": True}
    await client.aclose()


async def test_engine_client_maps_engine_error():
    def handler(_request):
        return httpx.Response(409, json={"code": "CYCLE_DEPENDENCY", "message": "存在环", "details": {"cycleNodes": ["a"]}})

    client = EngineClient("http://e.test", "/api", 5.0, transport=httpx.MockTransport(handler))
    with pytest.raises(ApiError) as info:
        await client.request("GET", "/bad")
    assert info.value.status_code == 409
    assert info.value.code == "CYCLE_DEPENDENCY"
    assert info.value.details == {"cycleNodes": ["a"]}
    await client.aclose()


async def test_engine_client_unreachable_maps_to_502():
    def handler(_request):
        raise httpx.ConnectError("connection refused")

    client = EngineClient("http://e.test", "/api", 5.0, transport=httpx.MockTransport(handler))
    with pytest.raises(ApiError) as info:
        await client.request("GET", "/plans")
    assert info.value.status_code == 502
    assert info.value.code == "ENGINE_UNAVAILABLE"
    await client.aclose()


async def test_tool_dispatcher_maps_tool_to_rest():
    seen: dict[str, str] = {}

    def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json={"data": {"criticalPath": ["t1"]}})

    client = EngineClient("http://e.test", "/api", 5.0, transport=httpx.MockTransport(handler))
    dispatcher = ToolDispatcher(client)
    result = await dispatcher.dispatch("schedule_calc", {"planId": "p1"})
    assert seen == {"method": "GET", "path": "/api/plans/p1/critical-path"}
    assert result == {"criticalPath": ["t1"]}
    await client.aclose()


async def test_tool_dispatcher_rejects_unknown_and_invalid_args():
    client = EngineClient("http://e.test", "/api", 5.0, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": {}})))
    dispatcher = ToolDispatcher(client)
    with pytest.raises(ApiError) as unknown:
        await dispatcher.dispatch("nonexistent", {})
    assert unknown.value.code == "VALIDATION_ERROR"
    with pytest.raises(ApiError):
        await dispatcher.dispatch("get_plan", {})  # 缺少 planId
    await client.aclose()
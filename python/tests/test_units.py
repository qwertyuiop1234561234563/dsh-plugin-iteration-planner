"""单元测试：WS 地址推导、错误信封、引擎客户端解包/映射、工具调度映射。"""

from __future__ import annotations

import json

import httpx
import pytest

from app.agent.llm import OpenAiCompatibleClient
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


# ---------------------- LLM 流式客户端（chat_stream） ----------------------


def _sse(payload: dict) -> str:
    """构造一行 OpenAI 兼容流式分片。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def test_llm_client_stream_aggregates_content_and_tool_calls():
    """流式聚合正确性：``content`` 增量逐帧产出；``tool_calls.arguments`` 分片须按 index 拼接。"""
    body = (
        _sse({"choices": [{"delta": {"content": "关键"}}]})
        + _sse({"choices": [{"delta": {"content": "路径"}}]})
        + _sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "id": "call_1", "type": "function", "function": {"name": "get_plan", "arguments": '{"plan'}}
                            ]
                        }
                    }
                ]
            }
        )
        + _sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'Id":"p1"}'}}]}}]})
        + "data: [DONE]\n\n"
    ).encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert json.loads(request.content.decode())["stream"] is True
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    client = OpenAiCompatibleClient("http://llm.test/v1", "k", "demo", transport=httpx.MockTransport(handler))
    try:
        texts: list[str] = []
        message: dict | None = None
        async for chunk in client.chat_stream([{"role": "user", "content": "hi"}]):
            if chunk["type"] == "content":
                texts.append(chunk["text"])
            else:
                message = chunk["message"]
    finally:
        await client.aclose()

    assert texts == ["关键", "路径"]
    assert message is not None
    assert message["content"] == "关键路径"
    call = message["tool_calls"][0]
    assert call["id"] == "call_1"
    assert call["function"]["name"] == "get_plan"
    # arguments 被切成两个分片，必须完整拼接后仍是合法 JSON
    assert json.loads(call["function"]["arguments"]) == {"planId": "p1"}


async def test_llm_client_stream_surfaces_upstream_error():
    """流式请求遇到 4xx：直接以 LLM_GATEWAY_ERROR 抛出（连接阶段的错误不得静默）。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "bad request"})

    client = OpenAiCompatibleClient("http://llm.test/v1", "k", "demo", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ApiError) as exc:
            async for _ in client.chat_stream([{"role": "user", "content": "hi"}]):
                pass  # pragma: no cover - 不应产出任何事件
        assert exc.value.code == "LLM_GATEWAY_ERROR"
    finally:
        await client.aclose()
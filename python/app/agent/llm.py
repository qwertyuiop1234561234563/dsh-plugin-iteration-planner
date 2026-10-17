"""LLM 客户端（OpenAI 兼容 /chat/completions）。

仅依赖 ``httpx``，不引入额外 SDK，便于在测试中以 MockTransport 注入。
支持标准的 function calling：请求携带 ``tools``，响应 message 中可能包含 ``tool_calls``。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Protocol

import httpx

from ..errors import ApiError, llm_gateway_error
from ..metrics import METRICS
from ..resilience import RetryPolicy, is_retryable_exception

logger = logging.getLogger("iteration_planner.llm")

#: 可重试的上游状态码。LLM 补全是无状态调用（重试不会产生副作用），因此与主流客户端
#: （openai-python 等的 ``_should_retry``）一致：429 限流 + **全部 5xx**（含 500——上游
#: 内部错误是最常见的瞬时故障，早期实现漏掉 500 会导致一次抖动就整轮失败）。
_RETRYABLE_LLM_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


class LlmClient(Protocol):
    """LLM 客户端协议（可替换为任意实现）。

    ``chat`` 为必备能力；``chat_stream`` 为**可选**流式能力，实现该方法的客户端可支持
    SSE 逐字输出（未实现时编排层自动回退为「一次性调用 + 单帧下发」，不影响功能）。
    """

    name: str

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        """返回 assistant 消息字典（含 ``content``、可选 ``tool_calls``）。"""
        ...


class OpenAiCompatibleClient:
    """OpenAI 兼容网关客户端（含连接池与指数退避重试）。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        connect_timeout: float = 10.0,
        max_connections: int = 50,
        max_keepalive_connections: int = 10,
        retry: RetryPolicy | None = None,
    ) -> None:
        self.name = f"openai-compatible:{model}"
        self._model = model
        self._retry = retry or RetryPolicy()
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(connect=connect_timeout, read=timeout, write=timeout, pool=connect_timeout),
            limits=httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_keepalive_connections),
            transport=transport,
            headers={"authorization": f"Bearer {api_key}"} if api_key else {},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        response = await self._post_with_retry(payload)

        try:
            body = response.json()
            message = body["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ApiError(422, "LLM_FORMAT_ERROR", f"LLM 输出格式非法：{exc}") from exc

        if not isinstance(message, dict):
            raise ApiError(422, "LLM_FORMAT_ERROR", "LLM 输出缺少 message 字段")
        return message

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
    ) -> AsyncIterator[dict[str, Any]]:
        """流式对话（``stream=true``，SSE 分片）。

        逐条产出两类事件：

        - ``{"type": "content", "text": str}``：助手文本增量（可直接下发给前端做打字机效果）；
        - ``{"type": "message", "message": dict}``：**末尾唯一一次**，aggregate 出的完整
          assistant 消息（含拼接好的 ``tool_calls``），供编排层做 function calling。

        重试语义：仅在**首个字节到达前**（连接阶段 / 上游 429、5xx）重试；一旦开始流式
        输出即不再重试——避免半截内容重复下发。
        """
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        ctx, response = await self._open_stream(payload)
        content_parts: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        try:
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:") :].strip()
                if not data:
                    continue
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    logger.debug("忽略无法解析的流式分片：%s", data[:200])
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                text = delta.get("content")
                if isinstance(text, str) and text:
                    if _is_protocol_leak(text):
                        # 上游偶发把 SSE 协议信封塞进 delta.content：既不是模型答复，也不能回灌历史
                        METRICS.inc_counter("llm_protocol_leak_total")
                        logger.warning("丢弃上游泄漏的 SSE 协议信封内容分片（%d 字符）", len(text))
                        continue
                    content_parts.append(text)
                    yield {"type": "content", "text": text}
                for fragment in delta.get("tool_calls") or []:
                    _merge_tool_fragment(tool_calls, fragment)
            METRICS.inc_counter("llm_requests_total", {"outcome": "success"})
        except httpx.HTTPError as exc:
            METRICS.inc_counter("llm_requests_total", {"outcome": "error"})
            raise llm_gateway_error(f"LLM 流式响应中断：{exc}") from exc
        finally:
            await ctx.__aexit__(None, None, None)

        raw_content = "".join(content_parts)
        content = strip_protocol_leak(raw_content)
        if content != raw_content:
            METRICS.inc_counter("llm_protocol_leak_total")
            logger.warning("剥离正文中混入的 SSE 协议信封（%d → %d 字符）", len(raw_content), len(content))
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            message["tool_calls"] = [_finalize_tool_call(index, value) for index, value in sorted(tool_calls.items())]
        yield {"type": "message", "message": message}

    async def _open_stream(self, payload: dict[str, Any]) -> tuple[Any, httpx.Response]:
        """打开流式响应；仅在首个字节到达前对「连接失败 / 上游 429、5xx」做指数退避重试。"""
        retry_index = 0
        while True:
            ctx = self._client.stream("POST", "/chat/completions", json=payload)
            try:
                response = await ctx.__aenter__()
            except httpx.HTTPError as exc:
                if retry_index < self._retry.attempts and is_retryable_exception(exc, "POST"):
                    METRICS.inc_counter("llm_requests_total", {"outcome": "retry_transport"})
                    await asyncio.sleep(self._retry.delay_for(retry_index))
                    retry_index += 1
                    continue
                METRICS.inc_counter("llm_requests_total", {"outcome": "unreachable"})
                raise llm_gateway_error(f"LLM 网关不可达：{exc}") from exc

            if response.status_code in _RETRYABLE_LLM_STATUS and retry_index < self._retry.attempts:
                await response.aread()
                await ctx.__aexit__(None, None, None)
                METRICS.inc_counter("llm_requests_total", {"outcome": "retry_status"})
                await asyncio.sleep(self._retry.delay_for(retry_index))
                retry_index += 1
                continue

            if response.status_code >= 400:
                raw = await response.aread()
                await ctx.__aexit__(None, None, None)
                METRICS.inc_counter("llm_requests_total", {"outcome": "error"})
                raise llm_gateway_error(
                    f"LLM 网关返回异常状态：{response.status_code}",
                    raw.decode("utf-8", "replace")[:1000],
                )
            return ctx, response

    async def _post_with_retry(self, payload: dict[str, Any]) -> httpx.Response:
        """带重试的请求：连接失败与 5xx / 429 可重试，4xx 直接失败。"""
        retry_index = 0
        while True:
            try:
                response = await self._client.post("/chat/completions", json=payload)
            except httpx.HTTPError as exc:
                if retry_index < self._retry.attempts and is_retryable_exception(exc, "POST"):
                    METRICS.inc_counter("llm_requests_total", {"outcome": "retry_transport"})
                    await asyncio.sleep(self._retry.delay_for(retry_index))
                    retry_index += 1
                    continue
                METRICS.inc_counter("llm_requests_total", {"outcome": "unreachable"})
                raise llm_gateway_error(f"LLM 网关不可达：{exc}") from exc

            # 429（限流）与 5xx 属于可恢复的上游压力，允许重试
            if response.status_code in _RETRYABLE_LLM_STATUS and retry_index < self._retry.attempts:
                METRICS.inc_counter("llm_requests_total", {"outcome": "retry_status"})
                await asyncio.sleep(self._retry.delay_for(retry_index))
                retry_index += 1
                continue

            if response.status_code >= 400:
                METRICS.inc_counter("llm_requests_total", {"outcome": "error"})
                raise llm_gateway_error(
                    f"LLM 网关返回异常状态：{response.status_code}",
                    response.text[:1000],
                )
            METRICS.inc_counter("llm_requests_total", {"outcome": "success"})
            return response


def _merge_tool_fragment(store: dict[int, dict[str, Any]], fragment: Any) -> None:
    """把 OpenAI 流式 ``tool_calls`` 分片按 ``index`` 合并。

    协议规定：``id`` / ``function.name`` 只在首个分片出现，``function.arguments`` 逐片拼接
    （模型按 token 输出 JSON 片段），因此必须按 ``index`` 累积而非整体覆盖。
    """
    if not isinstance(fragment, dict):
        return
    index = fragment.get("index")
    if not isinstance(index, int):
        index = 0
    slot = store.setdefault(index, {"id": None, "type": "function", "function": {"name": "", "arguments": ""}})
    if fragment.get("id"):
        slot["id"] = fragment["id"]
    if fragment.get("type"):
        slot["type"] = fragment["type"]
    function = fragment.get("function")
    if isinstance(function, dict):
        if function.get("name"):
            slot["function"]["name"] = function["name"]
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            slot["function"]["arguments"] += arguments


def _finalize_tool_call(index: int, value: dict[str, Any]) -> dict[str, Any]:
    """把聚合态补全为符合 OpenAI 协议的 tool_call（``arguments`` 缺省为 ``{}``）。"""
    function = value.get("function") or {}
    return {
        "id": value.get("id") or f"call_{index}",
        "type": value.get("type") or "function",
        "function": {
            "name": function.get("name") or "",
            "arguments": function.get("arguments") or "{}",
        },
    }


def _is_protocol_leak(text: str) -> bool:
    """判断一个 content 分片是否是上游误放的 SSE 协议信封（整块形态）。"""
    return "finish_reason" in text and '"delta"' in text


def strip_protocol_leak(text: str) -> str:
    """剥离上游混入 ``content`` 的 SSE 协议信封，返回纯助手文本。

    归一化流式实现偶发把**协议信封**当作助手文本下发，形如
    ``{"index":0,"finish_reason":"tool_calls","delta":{"role":"assistant",...}}``。
    这类字节既不是模型答复、也不能回灌进会话历史，且总是出现在正文尾部，
    因此在聚合后统一截断剥离（用 ``json.raw_decode`` 定位信封起点，避免把信封前的
    正常陈述一并丢弃）。编排层亦复用本函数作为不依赖具体客户端实现的最后防线。
    """
    if "finish_reason" not in text:
        return text
    decoder = json.JSONDecoder()
    cursor = 0
    while True:
        start = text.find('{"', cursor)
        if start < 0:
            return text
        try:
            obj, _ = decoder.raw_decode(text, start)
        except ValueError:
            cursor = start + 2
            continue
        if isinstance(obj, dict) and "finish_reason" in obj and "delta" in obj:
            return text[:start].rstrip()
        cursor = start + 2


__all__ = ["LlmClient", "OpenAiCompatibleClient", "strip_protocol_leak"]
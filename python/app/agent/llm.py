"""LLM 客户端（OpenAI 兼容 /chat/completions）。

仅依赖 ``httpx``，不引入额外 SDK，便于在测试中以 MockTransport 注入。
支持标准的 function calling：请求携带 ``tools``，响应 message 中可能包含 ``tool_calls``。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

import httpx

from ..errors import ApiError, bad_gateway
from ..metrics import METRICS
from ..resilience import RetryPolicy, is_retryable_exception

logger = logging.getLogger("iteration_planner.llm")


class LlmClient(Protocol):
    """LLM 客户端协议（可替换为任意实现）。"""

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
                raise bad_gateway(f"LLM 网关不可达：{exc}") from exc

            # 429（限流）与 5xx 属于可恢复的上游压力，允许重试
            if response.status_code in (429, 502, 503, 504) and retry_index < self._retry.attempts:
                METRICS.inc_counter("llm_requests_total", {"outcome": "retry_status"})
                await asyncio.sleep(self._retry.delay_for(retry_index))
                retry_index += 1
                continue

            if response.status_code >= 400:
                METRICS.inc_counter("llm_requests_total", {"outcome": "error"})
                raise ApiError(
                    502,
                    "LLM_GATEWAY_ERROR",
                    f"LLM 网关返回异常状态：{response.status_code}",
                    response.text[:1000],
                )
            METRICS.inc_counter("llm_requests_total", {"outcome": "success"})
            return response


__all__ = ["LlmClient", "OpenAiCompatibleClient"]
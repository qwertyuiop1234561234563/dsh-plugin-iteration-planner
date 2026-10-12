"""内部算法引擎客户端。

Python 不复刻任何算法：所有 DAG / AOE / 风险 / 报表计算都通过 HTTP 调用
TypeScript 引擎（dsh 插件 + Fastify），保证「算法单一事实来源」不发生漂移。

两类用法：
- :meth:`request`：面向 Agent 工具，自动解包 ``{"data": ...}`` 信封并把引擎错误
  原样映射为 :class:`ApiError`（保留相同的状态码与错误码）。
- :meth:`forward`：面向对外网关，逐字节透传（含导出文件、204 空响应等非 JSON 场景）。

韧性（企业级必需）：连接池上限 + 连接/读取分离超时 + 断路器 + 仅在安全前提下重试。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from .errors import ApiError, bad_gateway, engine_circuit_open
from .logging_config import request_id_var
from .metrics import METRICS
from .resilience import CircuitBreaker, RetryPolicy, is_retryable_exception, is_retryable_status

logger = logging.getLogger("iteration_planner.engine")


class EngineClient:
    """对 TypeScript 算法引擎的异步 HTTP 封装（含断路器与重试）。"""

    def __init__(
        self,
        base_url: str,
        base_path: str = "/api",
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        connect_timeout: float = 5.0,
        max_connections: int = 100,
        max_keepalive_connections: int = 20,
        retry: RetryPolicy | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._base_path = "/" + base_path.strip("/")
        self._retry = retry or RetryPolicy()
        self._breaker = breaker or CircuitBreaker()
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            # 连接建立用短超时快速失败；读取用业务超时（引擎重算可能较慢）
            timeout=httpx.Timeout(connect=connect_timeout, read=timeout, write=timeout, pool=connect_timeout),
            limits=httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=max_keepalive_connections,
            ),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def breaker_state(self) -> str:
        return self._breaker.state.value

    # --------------------------------- 通用 ---------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """调用引擎业务接口并解包 ``{data}`` 信封。

        失败时抛出与引擎语义一致的 :class:`ApiError`（同状态码、同错误码）。
        """
        response = await self._send(
            method,
            self._base_path + path,
            json=json,
            params=params,
            headers=headers,
        )

        if response.status_code >= 400:
            raise self._to_api_error(response)

        if response.status_code == 204 or not response.content:
            return None
        payload = response.json()
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    async def forward(
        self,
        method: str,
        path: str,
        *,
        params: httpx.QueryParams | dict[str, Any] | list[tuple[str, str]] | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """透传请求并返回原始响应，供对外网关镜像引擎契约。"""
        return await self._send(method, path, params=params, content=content, headers=headers)

    # --------------------------------- 内部 ---------------------------------

    async def _send(
        self,
        method: str,
        url: str,
        *,
        json: Any | None = None,
        params: Any | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """带断路器与重试的底层请求。

        重试安全前提（见 :mod:`app.resilience`）：连接阶段失败任何方法都可重试；
        读超时 / 5xx 仅幂等方法重试，避免 ``POST /plans`` 被重复执行。
        """
        if not self._breaker.allow():
            METRICS.inc_counter("engine_requests_total", {"method": method, "outcome": "circuit_open"})
            raise engine_circuit_open()

        merged_headers = self._merge_headers(headers)
        retry_index = 0
        while True:
            try:
                response = await self._client.request(
                    method,
                    url,
                    json=json,
                    params=params,
                    content=content,
                    headers=merged_headers,
                )
            except httpx.HTTPError as exc:  # 连接失败 / 超时
                if retry_index < self._retry.attempts and is_retryable_exception(exc, method):
                    METRICS.inc_counter("engine_request_retries_total", {"method": method, "reason": "transport"})
                    await asyncio.sleep(self._retry.delay_for(retry_index))
                    retry_index += 1
                    continue
                self._breaker.record_failure()
                METRICS.inc_counter("engine_requests_total", {"method": method, "outcome": "unreachable"})
                raise bad_gateway(f"算法引擎不可达：{exc}") from exc

            if retry_index < self._retry.attempts and is_retryable_status(response.status_code, method):
                METRICS.inc_counter("engine_request_retries_total", {"method": method, "reason": "status"})
                await asyncio.sleep(self._retry.delay_for(retry_index))
                retry_index += 1
                continue

            if response.status_code >= 500:
                self._breaker.record_failure()
                METRICS.inc_counter("engine_requests_total", {"method": method, "outcome": "server_error"})
            else:
                self._breaker.record_success()
                METRICS.inc_counter("engine_requests_total", {"method": method, "outcome": "success"})
            return response

    @staticmethod
    def _merge_headers(headers: dict[str, str] | None) -> dict[str, str]:
        """合并调用方首部与当前请求关联 ID（便于跨服务全链路追踪）。"""
        merged: dict[str, str] = dict(headers or {})
        request_id = request_id_var.get()
        if request_id:
            merged.setdefault("x-request-id", request_id)
        return merged

    @staticmethod
    def _to_api_error(response: httpx.Response) -> ApiError:
        """把引擎错误响应还原为 :class:`ApiError`（保持契约一致）。"""
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and "code" in body and "message" in body:
            return ApiError(
                response.status_code,
                str(body["code"]),
                str(body["message"]),
                body.get("details"),
            )
        return ApiError(
            response.status_code,
            "ENGINE_ERROR",
            f"算法引擎返回异常状态：{response.status_code}",
            response.text[:1000],
        )
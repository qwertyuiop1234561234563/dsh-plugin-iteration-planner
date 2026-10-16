"""文本向量化（Embedding）。

设计要点（对齐项目既有 LLM 网关的工程口径）：

- **协议化**：``Embedder`` 为 ``Protocol``，生产用 :class:`BailianEmbedder`，
  测试可注入桩实现（不触网、可复现）；
- **OpenAI 兼容**：调用 ``POST {base}/embeddings``，请求体 ``{model, input, dimensions}``，
  与阿里云百炼「兼容模式」一致（``text-embedding-v4``）；
- **分批**：百炼单次请求文本条数较少，按 ``batch_size``（默认 10）分批，避免 400；
- **韧性**：连接失败 / 429 / 5xx 指数退避重试，其余 4xx 立即失败（不放大错误）；
- **可观测**：按结果埋点 ``embedding_requests_total``，并统计耗时直方图。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol, Sequence

import httpx

from ..errors import embedding_gateway_error, embedding_unavailable
from ..metrics import METRICS
from ..resilience import RetryPolicy

logger = logging.getLogger("iteration_planner.embedding")


class Embedder(Protocol):
    """向量化客户端协议。"""

    name: str
    dimensions: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """把一批文本向量化，返回与输入**同序**的向量列表。"""
        ...

    async def aclose(self) -> None:
        """释放底层连接。"""
        ...


class BailianEmbedder:
    """阿里云百炼（OpenAI 兼容）向量化客户端。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        dimensions: int = 1024,
        batch_size: int = 10,
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
        connect_timeout: float = 10.0,
        max_connections: int = 20,
        max_keepalive_connections: int = 10,
        retry: RetryPolicy | None = None,
    ) -> None:
        self.name = f"bailian-embedding:{model}"
        self.dimensions = max(1, dimensions)
        self._model = model
        self._batch_size = max(1, min(batch_size, 25))
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

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """分批向量化，保持输入顺序。"""
        items = [text if text and text.strip() else " " for text in texts]
        if not items:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(items), self._batch_size):
            batch = items[start : start + self._batch_size]
            vectors.extend(await self._embed_batch(batch))
        return vectors

    # ------------------------------- 内部实现 -------------------------------

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        payload: dict[str, Any] = {
            "model": self._model,
            "input": batch,
            "encoding_format": "float",
        }
        # 部分模型（如 text-embedding-v3/v4）支持显式指定输出维度
        if self.dimensions:
            payload["dimensions"] = self.dimensions

        started = asyncio.get_running_loop().time()
        response = await self._post_with_retry(payload)
        METRICS.observe(
            "embedding_request_duration_seconds",
            asyncio.get_running_loop().time() - started,
            help="向量化请求耗时（秒）",
        )

        try:
            body = response.json()
            rows = body["data"]
        except (ValueError, KeyError, TypeError) as exc:
            raise embedding_gateway_error(f"向量化输出格式非法：{exc}", response.text[:500]) from exc
        if not isinstance(rows, list) or len(rows) != len(batch):
            raise embedding_gateway_error(
                f"向量化返回条数与输入不一致（输入 {len(batch)}，返回 {len(rows) if isinstance(rows, list) else 'N/A'}）"
            )

        ordered = sorted(rows, key=lambda row: int(row.get("index", 0)) if isinstance(row, dict) else 0)
        vectors: list[list[float]] = []
        for row in ordered:
            embedding = row.get("embedding") if isinstance(row, dict) else None
            if not isinstance(embedding, list) or not embedding:
                raise embedding_gateway_error("向量化返回缺少 embedding 字段")
            if self.dimensions and len(embedding) != self.dimensions:
                raise embedding_gateway_error(
                    f"向量维度与配置不一致（配置 {self.dimensions}，实际 {len(embedding)}）；"
                    "请核对 EMBEDDING_DIMENSIONS 与模型支持的维度档位"
                )
            vectors.append([float(value) for value in embedding])
        return vectors

    async def _post_with_retry(self, payload: dict[str, Any]) -> httpx.Response:
        retry_index = 0
        while True:
            try:
                response = await self._client.post("/embeddings", json=payload)
            except httpx.HTTPError as exc:
                if retry_index < self._retry.attempts:
                    METRICS.inc_counter("embedding_requests_total", {"outcome": "retry_transport"})
                    await asyncio.sleep(self._retry.delay_for(retry_index))
                    retry_index += 1
                    continue
                METRICS.inc_counter("embedding_requests_total", {"outcome": "unreachable"})
                raise embedding_unavailable(f"向量化服务不可达：{exc}") from exc

            if response.status_code in (429, 500, 502, 503, 504) and retry_index < self._retry.attempts:
                METRICS.inc_counter("embedding_requests_total", {"outcome": "retry_status"})
                await asyncio.sleep(self._retry.delay_for(retry_index))
                retry_index += 1
                continue

            if response.status_code >= 400:
                METRICS.inc_counter("embedding_requests_total", {"outcome": "error"})
                raise embedding_gateway_error(
                    f"向量化服务返回异常状态：{response.status_code}",
                    response.text[:1000],
                )
            METRICS.inc_counter("embedding_requests_total", {"outcome": "success"})
            return response


__all__ = ["Embedder", "BailianEmbedder"]

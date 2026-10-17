"""文本向量化（Embedding）。

设计要点（对齐项目既有 LLM 网关的工程口径）：

- **协议化**：``Embedder`` 为 ``Protocol``，生产用 :class:`OpenAiEmbedder`，
  测试可注入桩实现（不触网、可复现）；
- **OpenAI 兼容**：调用 ``POST {base}/embeddings``，请求体 ``{model, input, dimensions}``，
  与智谱 GLM「OpenAI 兼容」端点一致（``embedding-3``，维度 256–2048 可自定义）；
- **分批**：embedding-3 单次请求最多 64 条文本，按 ``batch_size``（默认 10）分批，避免 400；
- **韧性**：连接失败 / 429 / 5xx 指数退避重试，其余 4xx 立即失败（不放大错误）；
- **可观测**：按结果埋点 ``embedding_requests_total``，并统计耗时直方图。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from typing import Any, Protocol, Sequence

import httpx

from ..cache import CacheBackend
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


class OpenAiEmbedder:
    """OpenAI 兼容向量化客户端（默认对接智谱 ``embedding-3``）。"""

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
        self.name = f"openai-embedding:{model}"
        self.dimensions = max(1, dimensions)
        self._model = model
        self._batch_size = max(1, min(batch_size, 64))
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
        }
        # embedding-3 支持显式指定输出维度（256–2048），按配置下发；不指定则用模型默认维度
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


class CachedEmbedder:
    """向量化结果的缓存包装（Redis / 进程内存），含同批去重与在途合并。

    **为什么可以缓存**：向量化是确定性函数——同一模型 + 同一维度 + 同一文本必然得到
    同一向量，缓存不会引入陈旧数据（缓存键已含模型名与维度，切换模型不会命中旧向量）。

    **为什么值得缓存**：一轮对话中「长期记忆召回」与「知识库检索」会对**同一段用户输入**
    各向量化一次，第二次直接命中，省掉一次上游往返；多副本部署时经 Redis 共享，
    同一问题被不同副本 / 不同用户重复提问时命中率不随副本数稀释。

    **在途合并**：相同一批文本的并发请求共用同一个 ``asyncio.Task``（``shield`` 保护，
    单个调用方取消不影响其它调用方），避免上游被重复风暴击穿。
    """

    def __init__(self, inner: Embedder, cache: CacheBackend, *, ttl_seconds: float = 3600.0) -> None:
        self._inner = inner
        self._cache = cache
        self._ttl = ttl_seconds
        self._inflight: dict[tuple[str, ...], asyncio.Task[list[list[float]]]] = {}

    @property
    def name(self) -> str:
        return f"{self._inner.name}+cache"

    @property
    def dimensions(self) -> int:
        return self._inner.dimensions

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """与底层实现**同序等长**返回；命中缓存的条目不再请求上游。"""
        items = [text if text and text.strip() else " " for text in texts]
        if not items:
            return []

        keys = [self._digest(text) for text in items]
        resolved: list[list[float] | None] = [None] * len(items)

        # 1) 批量查缓存（重复键重复查询无妨，命中即复用）
        cached_values = await self._cache.get_many(keys)
        missing_text: dict[str, str] = {}
        for index, key in enumerate(keys):
            value = cached_values[index] if index < len(cached_values) else None
            if self._is_valid_vector(value):
                resolved[index] = value  # type: ignore[assignment]
                continue
            missing_text.setdefault(key, items[index])

        # 2) 未命中的按「去重后的顺序」批量取回，并回填缓存
        if missing_text:
            ordered_keys = tuple(missing_text)
            vectors = await self._fetch_coalesced(ordered_keys, [missing_text[key] for key in ordered_keys])
            for index, key in enumerate(keys):
                vector = vectors.get(key)
                if vector is None:
                    continue
                if resolved[index] is None:
                    resolved[index] = vector
                await self._cache.set(key, vector, self._ttl)

        # 3) 契约校验：与底层实现一致，条数不足必须显式失败（不静默返回残缺结果）
        if any(vector is None for vector in resolved):
            raise embedding_gateway_error("向量化结果条数与输入不一致（缓存包装层校验）")
        return [vector for vector in resolved if vector is not None]

    # ------------------------------- 内部实现 -------------------------------

    def _digest(self, text: str) -> str:
        """缓存键：模型 + 维度 + 文本（三者任一变化即不命中，天然避免陈旧向量）。"""
        raw = f"{self._inner.name}|{self._inner.dimensions}|{text}".encode()
        return f"embedding:{hashlib.sha256(raw).hexdigest()}"

    def _is_valid_vector(self, value: Any) -> bool:
        """缓存值须为与配置维度一致的浮点数组，否则按未命中处理。"""
        if not isinstance(value, list) or not value:
            return False
        if len(value) != self._inner.dimensions:
            return False
        return all(isinstance(item, (int, float)) for item in value)

    async def _fetch_coalesced(self, batch_key: tuple[str, ...], texts: list[str]) -> dict[str, list[float]]:
        """取回一批未命中文本的向量；相同批次并发调用共享同一底层请求。"""
        task = self._inflight.get(batch_key)
        if task is None:
            task = asyncio.create_task(self._inner.embed(texts), name="embedding-fetch")
            self._inflight[batch_key] = task
            task.add_done_callback(lambda done: self._finish(batch_key, done))
        vectors = await asyncio.shield(task)
        return dict(zip(batch_key, vectors))

    def _finish(self, batch_key: tuple[str, ...], task: asyncio.Task[list[list[float]]]) -> None:
        """清理在途登记，并消费异常避免「Future exception was never retrieved」告警。"""
        if self._inflight.get(batch_key) is not task:
            return
        self._inflight.pop(batch_key, None)
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.exception()


__all__ = ["Embedder", "OpenAiEmbedder", "CachedEmbedder"]

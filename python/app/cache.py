"""可插拔缓存（Redis / 进程内存）。

用途：缓存**确定性的昂贵计算结果**——当前用于文本向量化（见
``app/kb/embedder.py`` 的 ``CachedEmbedder``）。收益：

- 同一轮对话里「长期记忆召回」与「知识库检索」会对**同一段用户输入**各向量化一次，
  第二次直接命中缓存，省掉一次上游往返（问答更快）；
- 重复提问 / 高并发相同提问直接命中，命中率随使用量提升；
- 多副本部署经 Redis 共享，命中率不随副本数稀释。

设计取舍（与 :mod:`app.sessions` 的 Redis 语义刻意不同，务必区分）：

- **fail-open**：缓存是纯优化。Redis 不可达时按「未命中」处理并计数告警，
  绝不抛错、绝不阻塞业务；而会话存储是**数据**，不可用时必须明确失败（503）。
- **只缓存确定性结果**：同一模型 + 同一输入 → 同一向量，因此无需版本化失效，
  仅用 TTL 控制内存占用（模型名与维度已包含在缓存键中，切换模型不会命中旧向量）。
- **不引入新运行时依赖**：未安装 ``redis`` 或未配置 ``REDIS_URL`` 时自动退化为
  进程内 LRU 缓存，行为不中断（仅失去跨副本共享）。
- **不允许成为新的时延瓶颈**：Redis 客户端刻意设置较短的连接 / 读写超时，
  故障时快速失败并降级，而不是把问答链路挂在 Redis 上。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import OrderedDict
from typing import Any, Protocol, Sequence, runtime_checkable

from .metrics import METRICS

logger = logging.getLogger("iteration_planner.cache")

#: 缓存键前缀（与 session 前缀区分，便于运维按前缀清理 / 观测）
DEFAULT_CACHE_PREFIX = "iteration_planner:cache:"


@runtime_checkable
class CacheBackend(Protocol):
    """缓存后端接口（值需可 JSON 序列化）。"""

    @property
    def name(self) -> str:
        """后端标识（memory / redis），用于日志与指标标签。"""
        ...

    async def get(self, key: str) -> Any | None:
        """读取；未命中 / 已过期 / 后端异常一律返回 ``None``。"""
        ...

    async def get_many(self, keys: Sequence[str]) -> list[Any | None]:
        """批量读取，返回与输入**等长同序**的结果（未命中为 ``None``）。"""
        ...

    async def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        """写入（``ttl_seconds <= 0`` 表示不缓存）。"""
        ...

    async def delete(self, *keys: str) -> None:
        ...

    async def close(self) -> None:
        ...

    def describe(self) -> dict[str, Any]:
        """运行状态快照（供 ``/api/health`` 观测命中率与容量）。"""
        ...


class _CacheStats:
    """命中 / 未命中 / 异常计数（两个后端共用）。"""

    def __init__(self, backend: str) -> None:
        self.backend = backend
        self.hits = 0
        self.misses = 0
        self.errors = 0

    def record_hit(self) -> None:
        self.hits += 1
        METRICS.inc_counter(
            "cache_operations_total",
            {"backend": self.backend, "outcome": "hit"},
            help="缓存读写次数（按后端与结果分标签）",
        )

    def record_miss(self) -> None:
        self.misses += 1
        METRICS.inc_counter("cache_operations_total", {"backend": self.backend, "outcome": "miss"})

    def record_error(self) -> None:
        self.errors += 1
        METRICS.inc_counter("cache_operations_total", {"backend": self.backend, "outcome": "error"})

    def snapshot(self) -> dict[str, Any]:
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "errors": self.errors,
            "hitRate": round(self.hits / total, 4) if total else None,
        }


class InMemoryCache:
    """进程内 TTL + LRU 缓存（单进程部署 / 未配置 Redis 时的默认后端）。

    单事件循环内使用，写入临界区不含 ``await``，因此无需加锁。
    """

    def __init__(self, *, max_entries: int = 2048) -> None:
        self._max_entries = max(1, max_entries)
        self._items: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._stats = _CacheStats("memory")
        self._evictions = 0

    @property
    def name(self) -> str:
        return "memory"

    async def get(self, key: str) -> Any | None:
        entry = self._items.get(key)
        if entry is None:
            self._stats.record_miss()
            return None
        value, expires_at = entry
        if expires_at <= time.monotonic():
            self._items.pop(key, None)
            self._stats.record_miss()
            return None
        self._items.move_to_end(key)
        self._stats.record_hit()
        return value

    async def get_many(self, keys: Sequence[str]) -> list[Any | None]:
        return [await self.get(key) for key in keys]

    async def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            return
        self._items[key] = (value, time.monotonic() + ttl_seconds)
        self._items.move_to_end(key)
        while len(self._items) > self._max_entries:
            self._items.popitem(last=False)
            self._evictions += 1

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self._items.pop(key, None)

    async def close(self) -> None:
        self._items.clear()

    def describe(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "entries": len(self._items),
            "maxEntries": self._max_entries,
            "evictions": self._evictions,
            **self._stats.snapshot(),
        }


class RedisCache:
    """Redis 缓存后端（多副本共享）。

    ``socket_timeout`` / ``socket_connect_timeout`` 设得较短：缓存必须**快速失败**，
    不允许 Redis 抖动把问答链路拖慢。所有异常都在此消化为「未命中」。
    """

    def __init__(
        self,
        url: str,
        *,
        prefix: str = DEFAULT_CACHE_PREFIX,
        socket_timeout: float = 1.0,
        connect_timeout: float = 1.0,
        error_log_interval: float = 30.0,
    ) -> None:
        from redis.asyncio import Redis  # type: ignore[import-not-found]

        self._redis = Redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=socket_timeout,
            socket_connect_timeout=connect_timeout,
        )
        self._prefix = prefix
        self._stats = _CacheStats("redis")
        self._error_log_interval = max(1.0, error_log_interval)
        self._last_error_log_at = 0.0
        self._suppressed_errors = 0

    @property
    def name(self) -> str:
        return "redis"

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    async def get(self, key: str) -> Any | None:
        values = await self.get_many([key])
        return values[0]

    async def get_many(self, keys: Sequence[str]) -> list[Any | None]:
        if not keys:
            return []
        try:
            raws = await self._redis.mget([self._key(key) for key in keys])
        except Exception as exc:  # noqa: BLE001 - fail-open：缓存故障不影响业务
            self._record_error("mget", exc)
            return [None] * len(keys)

        out: list[Any | None] = []
        for raw in raws:
            if raw is None:
                self._stats.record_miss()
                out.append(None)
                continue
            try:
                out.append(json.loads(raw))
            except (ValueError, TypeError):
                # 脏数据（如手工写入 / 版本不符）：按未命中处理，交由调用方重新计算后覆盖
                self._stats.record_miss()
                out.append(None)
                continue
            self._stats.record_hit()
        return out

    async def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            return
        try:
            payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            await self._redis.set(self._key(key), payload, ex=max(1, int(ttl_seconds)))
        except Exception as exc:  # noqa: BLE001
            self._record_error("set", exc)

    async def delete(self, *keys: str) -> None:
        if not keys:
            return
        try:
            await self._redis.delete(*[self._key(key) for key in keys])
        except Exception as exc:  # noqa: BLE001
            self._record_error("delete", exc)

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._redis.aclose()

    def describe(self) -> dict[str, Any]:
        return {"backend": self.name, **self._stats.snapshot()}

    # ------------------------------- 内部实现 -------------------------------

    def _record_error(self, operation: str, exc: BaseException) -> None:
        """登记缓存故障：计数 + 限频日志（避免 Redis 长时间不可用时刷屏）。"""
        self._stats.record_error()
        now = time.monotonic()
        if now - self._last_error_log_at < self._error_log_interval:
            self._suppressed_errors += 1
            return
        suppressed, self._suppressed_errors = self._suppressed_errors, 0
        self._last_error_log_at = now
        logger.warning(
            "缓存后端（Redis）%s 失败，已降级为未命中（近 %.0fs 内另有 %d 次同类错误）：%s",
            operation,
            self._error_log_interval,
            suppressed,
            exc,
        )


def build_cache(
    *,
    driver: str = "auto",
    redis_url: str = "",
    max_entries: int = 2048,
    prefix: str = DEFAULT_CACHE_PREFIX,
) -> CacheBackend:
    """按配置构建缓存后端。

    :param driver: ``auto``（有 ``REDIS_URL`` 且依赖可用则用 Redis，否则进程内存）|
        ``memory`` | ``redis``
    :param redis_url: ``REDIS_URL``；``driver=redis`` 时为必填
    :param max_entries: 进程内后端的 LRU 上限
    :raises RuntimeError: ``driver=redis`` 但缺少 ``REDIS_URL`` 或未安装 ``redis`` 依赖
        （显式选择 Redis 时不静默降级，避免「以为共享了缓存其实各存各的」）
    """
    choice = (driver or "auto").strip().lower()
    use_redis = choice == "redis" or (choice == "auto" and bool(redis_url))
    if not use_redis:
        logger.info("缓存后端：进程内存（LRU 上限 %d 项）", max_entries)
        return InMemoryCache(max_entries=max_entries)
    if not redis_url:
        raise RuntimeError("CACHE_BACKEND=redis 但未配置 REDIS_URL")
    try:
        cache: CacheBackend = RedisCache(redis_url, prefix=prefix)
    except ImportError as exc:
        if choice == "redis":
            raise RuntimeError(
                "未安装可选依赖 redis，无法启用 CACHE_BACKEND=redis；请执行 `pip install redis` 或改用 memory"
            ) from exc
        logger.warning("未安装可选依赖 redis，缓存自动降级为进程内存")
        return InMemoryCache(max_entries=max_entries)
    logger.info("缓存后端：Redis（%s）", redis_url)
    return cache


__all__ = ["CacheBackend", "InMemoryCache", "RedisCache", "build_cache", "DEFAULT_CACHE_PREFIX"]

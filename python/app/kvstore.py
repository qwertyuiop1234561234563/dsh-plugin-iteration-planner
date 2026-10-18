"""通用键值存储（作业状态 / 幂等键）。

与 :mod:`app.cache` 的区别（务必区分，否则会写错失败语义）：

- ``cache`` 缓存的是**可重算的确定性结果**，因此后端故障一律 **fail-open**（按未命中处理）；
- ``kvstore`` 存的是**状态本身**（作业进度、幂等键占位），是**数据**：后端故障必须
  显式失败，否则会出现「返回了 202 + jobId，但该作业其实不存在」这类客户端永远
  轮询不到的假成功。因此本模块在运行期故障时抛 :class:`KvStoreError`，由调用方
  决定语义（作业接口 → 503；幂等 → 明确降级并在响应中标注）。

其余设计沿用 :mod:`app.cache` 的既有范式：``Protocol`` + 内存实现 + Redis 实现 +
``build_*`` 工厂 + ``describe()``；``driver=redis`` 但缺少 ``REDIS_URL`` 或未安装
``redis`` 依赖时 **启动即失败**（不静默降级成「各副本各存各的」）。
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from collections import OrderedDict
from typing import Any, Protocol, runtime_checkable

from .metrics import METRICS

logger = logging.getLogger("iteration_planner.kvstore")


class KvStoreError(RuntimeError):
    """键值存储不可用（纯运行期故障；配置错误在构建期就应失败）。"""

    def __init__(self, store: str, operation: str, cause: BaseException) -> None:
        super().__init__(f"{store} 存储不可用（{operation}）")
        self.store = store
        self.operation = operation
        self.cause = cause


@runtime_checkable
class KeyValueStore(Protocol):
    """键值存储接口（值需可 JSON 序列化）。"""

    @property
    def name(self) -> str:
        """后端标识（memory / redis）。"""
        ...

    async def get(self, key: str) -> Any | None:
        """读取；不存在 / 已过期返回 ``None``；后端故障抛 :class:`KvStoreError`。"""
        ...

    async def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        """写入（覆盖）。"""
        ...

    async def set_nx(self, key: str, value: Any, ttl_seconds: float) -> bool:
        """**原子**「不存在才写入」；返回是否写入成功。

        这是幂等键与作业抢占的基础原语：必须由存储层保证原子性，进程内加锁在
        多副本部署下完全无效（各副本各有一把锁）。
        """
        ...

    async def delete(self, *keys: str) -> None:
        ...

    async def close(self) -> None:
        ...

    def describe(self) -> dict[str, Any]:
        ...


class _KvStats:
    def __init__(self, store: str, backend: str) -> None:
        self.store = store
        self.backend = backend
        self.hits = 0
        self.misses = 0
        self.writes = 0
        self.nx_conflicts = 0
        self.errors = 0

    def record(self, operation: str, outcome: str) -> None:
        METRICS.inc_counter(
            "kv_store_operations_total",
            {"store": self.store, "backend": self.backend, "operation": operation, "outcome": outcome},
            help="键值存储读写次数（按用途 / 后端 / 操作 / 结果分标签）",
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "writes": self.writes,
            "nxConflicts": self.nx_conflicts,
            "errors": self.errors,
        }


class InMemoryKV:
    """进程内 TTL + LRU 键值存储（单副本部署的默认后端）。

    单事件循环内使用，写入临界区不含 ``await``，因此无需加锁。
    """

    def __init__(self, *, store: str, max_entries: int = 1024) -> None:
        self._store = store
        self._max_entries = max(1, max_entries)
        self._items: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._stats = _KvStats(store, "memory")
        self._evictions = 0

    @property
    def name(self) -> str:
        return "memory"

    def _live(self, key: str) -> tuple[Any, float] | None:
        entry = self._items.get(key)
        if entry is None:
            return None
        _, expires_at = entry
        if expires_at <= time.monotonic():
            self._items.pop(key, None)
            return None
        return entry

    async def get(self, key: str) -> Any | None:
        entry = self._live(key)
        if entry is None:
            self._stats.misses += 1
            self._stats.record("get", "miss")
            return None
        self._items.move_to_end(key)
        self._stats.hits += 1
        self._stats.record("get", "hit")
        return entry[0]

    async def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            return
        self._items[key] = (value, time.monotonic() + ttl_seconds)
        self._items.move_to_end(key)
        self._stats.writes += 1
        self._stats.record("set", "success")
        while len(self._items) > self._max_entries:
            self._items.popitem(last=False)
            self._evictions += 1

    async def set_nx(self, key: str, value: Any, ttl_seconds: float) -> bool:
        if self._live(key) is not None:
            self._stats.nx_conflicts += 1
            self._stats.record("set_nx", "conflict")
            return False
        await self.set(key, value, ttl_seconds)
        return True

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self._items.pop(key, None)
        if keys:
            self._stats.record("delete", "success")

    async def close(self) -> None:
        self._items.clear()

    def describe(self) -> dict[str, Any]:
        return {
            "store": self._store,
            "backend": self.name,
            "entries": len(self._items),
            "maxEntries": self._max_entries,
            "evictions": self._evictions,
            **self._stats.snapshot(),
        }


class RedisKV:
    """Redis 键值存储（多副本共享；作业与幂等键在副本间可见）。

    ``socket_timeout`` 较短：存储故障时要**快速失败**，不允许把请求挂在 Redis 上。
    与缓存不同，这里**不吞异常**：故障以 :class:`KvStoreError` 上抛，由调用方决定
    语义（作业 → 503；幂等 → 降级放行并标注）。
    """

    def __init__(
        self,
        url: str,
        *,
        store: str,
        prefix: str,
        socket_timeout: float = 1.5,
        connect_timeout: float = 1.5,
        error_log_interval: float = 30.0,
    ) -> None:
        from redis.asyncio import Redis  # type: ignore[import-not-found]

        self._redis = Redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=socket_timeout,
            socket_connect_timeout=connect_timeout,
        )
        self._store = store
        self._prefix = prefix
        self._stats = _KvStats(store, "redis")
        self._error_log_interval = max(1.0, error_log_interval)
        self._last_error_log_at = 0.0

    @property
    def name(self) -> str:
        return "redis"

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    async def get(self, key: str) -> Any | None:
        try:
            raw = await self._redis.get(self._key(key))
        except Exception as exc:  # noqa: BLE001 - 状态存储故障必须显式失败
            self._fail("get", exc)
        if raw is None:
            self._stats.misses += 1
            self._stats.record("get", "miss")
            return None
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            # 脏数据（版本不符 / 手工写入）：按不存在处理，由调用方重建
            self._stats.misses += 1
            self._stats.record("get", "corrupt")
            return None
        self._stats.hits += 1
        self._stats.record("get", "hit")
        return value

    async def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            return
        try:
            payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            await self._redis.set(self._key(key), payload, ex=max(1, int(ttl_seconds)))
        except Exception as exc:  # noqa: BLE001
            self._fail("set", exc)
        self._stats.writes += 1
        self._stats.record("set", "success")

    async def set_nx(self, key: str, value: Any, ttl_seconds: float) -> bool:
        if ttl_seconds <= 0:
            return False
        try:
            payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            # ``SET key value NX EX ttl`` 由 Redis 单线程原子执行：
            # 多副本并发下只有一个调用者能拿到写权，这是幂等键正确性的前提。
            created = await self._redis.set(self._key(key), payload, ex=max(1, int(ttl_seconds)), nx=True)
        except Exception as exc:  # noqa: BLE001
            self._fail("set_nx", exc)
        if created:
            self._stats.writes += 1
            self._stats.record("set_nx", "success")
            return True
        self._stats.nx_conflicts += 1
        self._stats.record("set_nx", "conflict")
        return False

    async def delete(self, *keys: str) -> None:
        if not keys:
            return
        try:
            await self._redis.delete(*[self._key(key) for key in keys])
        except Exception as exc:  # noqa: BLE001
            self._fail("delete", exc)
        self._stats.record("delete", "success")

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._redis.aclose()

    def describe(self) -> dict[str, Any]:
        return {"store": self._store, "backend": self.name, **self._stats.snapshot()}

    # ------------------------------- 内部实现 -------------------------------

    def _fail(self, operation: str, exc: BaseException) -> None:
        self._stats.errors += 1
        self._stats.record(operation, "error")
        now = time.monotonic()
        if now - self._last_error_log_at >= self._error_log_interval:
            self._last_error_log_at = now
            logger.warning("键值存储（Redis）%s 失败：%s", operation, exc)
        raise KvStoreError(self._store, operation, exc) from exc


def build_kv_store(
    *,
    store: str,
    driver: str = "auto",
    redis_url: str = "",
    max_entries: int = 1024,
    prefix: str,
) -> KeyValueStore:
    """按配置构建键值存储。

    :param store: 用途标签（``job`` / ``idempotency``），用于指标与日志区分，不影响数据。
    :param driver: ``auto``（有 ``REDIS_URL`` 且依赖可用则 Redis，否则进程内存）| ``memory`` | ``redis``
    :raises RuntimeError: ``driver=redis`` 但缺少 ``REDIS_URL`` 或未安装 ``redis`` 依赖 ——
        显式选择 Redis 时不静默降级，避免「以为多副本共享了状态其实各存各的」。
    """
    choice = (driver or "auto").strip().lower()
    use_redis = choice == "redis" or (choice == "auto" and bool(redis_url))
    if not use_redis:
        logger.info("%s 存储后端：进程内存（LRU 上限 %d 项）", store, max_entries)
        return InMemoryKV(store=store, max_entries=max_entries)
    if not redis_url:
        raise RuntimeError(f"{store.upper()}_STORE=redis 但未配置 REDIS_URL")
    try:
        kv: KeyValueStore = RedisKV(redis_url, store=store, prefix=prefix)
    except ImportError as exc:
        if choice == "redis":
            raise RuntimeError(
                f"未安装可选依赖 redis，无法启用 {store.upper()}_STORE=redis；"
                "请执行 `pip install redis` 或改用 memory"
            ) from exc
        logger.warning("未安装可选依赖 redis，%s 存储自动降级为进程内存", store)
        return InMemoryKV(store=store, max_entries=max_entries)
    logger.info("%s 存储后端：Redis（%s）", store, redis_url)
    return kv


__all__ = ["KeyValueStore", "InMemoryKV", "RedisKV", "KvStoreError", "build_kv_store"]

"""会话存储（可插拔）。

Agent 的会话式对话需要跨请求保留上下文。企业级要求：

- **可插拔**：默认进程内存（带 TTL 与 LRU 上限），配置 ``REDIS_URL`` 后可切到 Redis
  （多副本 / 多 worker 共享会话）；
- **并发安全**：同一 ``session_id`` 的请求串行化（会话级锁），避免并发写入相互覆盖；
- **自清理**：内存实现提供后台过期清理任务，避免长跑进程内存泄漏。

Redis 为**可选依赖**：仅在 ``SESSION_STORE=redis`` 时惰性导入 ``redis``，未安装则给出
明确报错（不静默降级），避免「以为共享了会话其实各存各的」这类隐蔽故障。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import OrderedDict
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger("iteration_planner.sessions")


@runtime_checkable
class SessionStore(Protocol):
    """会话存储接口。"""

    async def get(self, session_id: str) -> list[dict[str, Any]] | None:
        """读取会话历史；不存在或已过期返回 ``None``。"""
        ...

    async def set(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        """写入 / 覆盖会话历史。"""
        ...

    async def get_pending(self, session_id: str) -> list[dict[str, Any]] | None:
        """读取会话的「待确认写操作」；不存在或已过期返回 ``None``。"""
        ...

    async def set_pending(self, session_id: str, actions: list[dict[str, Any]]) -> None:
        """写入 / 覆盖待确认写操作（空列表表示清空）。"""
        ...

    async def pop(self, session_id: str) -> bool:
        """删除会话（含待确认动作），返回是否存在。"""
        ...

    def lock(self, session_id: str) -> "asyncio.Lock":
        """返回会话级锁（同 session 串行化）。"""
        ...

    async def start(self) -> None:
        """启动（如后台清理任务）。"""
        ...

    async def close(self) -> None:
        """释放资源。"""
        ...

    async def size(self) -> int:
        ...


class InMemorySessionStore:
    """进程内存会话存储（LRU + TTL + 会话级锁）。"""

    def __init__(self, *, max_sessions: int = 200, ttl_seconds: int = 3600) -> None:
        self._max_sessions = max(1, max_sessions)
        self._ttl = ttl_seconds
        self._sessions: OrderedDict[str, tuple[list[dict[str, Any]], float]] = OrderedDict()
        #: 待确认写操作（与历史分开存放：它不是消息，不能污染送入模型的上下文）
        self._pending: OrderedDict[str, tuple[list[dict[str, Any]], float]] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        self._cleanup_task: asyncio.Task[None] | None = None

    def _is_expired(self, updated_at: float, now: float) -> bool:
        return self._ttl > 0 and (now - updated_at) > self._ttl

    async def get(self, session_id: str) -> list[dict[str, Any]] | None:
        entry = self._sessions.get(session_id)
        if entry is None:
            return None
        messages, updated_at = entry
        if self._is_expired(updated_at, time.monotonic()):
            self._sessions.pop(session_id, None)
            return None
        self._sessions.move_to_end(session_id)
        return messages

    async def set(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        self._sessions[session_id] = (messages, time.monotonic())
        self._sessions.move_to_end(session_id)
        while len(self._sessions) > self._max_sessions:
            self._sessions.popitem(last=False)

    async def get_pending(self, session_id: str) -> list[dict[str, Any]] | None:
        entry = self._pending.get(session_id)
        if entry is None:
            return None
        actions, updated_at = entry
        if self._is_expired(updated_at, time.monotonic()):
            self._pending.pop(session_id, None)
            return None
        self._pending.move_to_end(session_id)
        return actions

    async def set_pending(self, session_id: str, actions: list[dict[str, Any]]) -> None:
        if not actions:
            self._pending.pop(session_id, None)
            return
        self._pending[session_id] = (actions, time.monotonic())
        self._pending.move_to_end(session_id)
        while len(self._pending) > self._max_sessions:
            self._pending.popitem(last=False)

    async def pop(self, session_id: str) -> bool:
        self._locks.pop(session_id, None)
        self._pending.pop(session_id, None)
        return self._sessions.pop(session_id, None) is not None

    def lock(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    async def size(self) -> int:
        return len(self._sessions)

    async def start(self) -> None:
        if self._cleanup_task is None and self._ttl > 0:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop(), name="session-cleanup")

    async def close(self) -> None:
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task
            self._cleanup_task = None

    async def _cleanup_loop(self, interval: float = 60.0) -> None:
        """周期性清理过期会话与空闲锁。"""
        while True:
            await asyncio.sleep(interval)
            now = time.monotonic()
            expired = [sid for sid, (_, ts) in self._sessions.items() if self._is_expired(ts, now)]
            for sid in expired:
                self._sessions.pop(sid, None)
            expired_pending = [sid for sid, (_, ts) in self._pending.items() if self._is_expired(ts, now)]
            for sid in expired_pending:
                self._pending.pop(sid, None)
            idle_locks = [sid for sid, lock in self._locks.items() if not lock.locked() and sid not in self._sessions]
            for sid in idle_locks:
                self._locks.pop(sid, None)
            if expired or expired_pending:
                logger.info("会话清理：移除 %d 个过期会话 / %d 份待确认动作", len(expired), len(expired_pending))


class RedisSessionStore:
    """Redis 会话存储（多副本共享；需安装可选依赖 ``redis`` 并配置 ``REDIS_URL``）。

    仅在 ``SESSION_STORE=redis`` 时启用；未安装 ``redis`` 时抛出明确错误，不静默降级。
    """

    def __init__(self, url: str, *, ttl_seconds: int = 3600, prefix: str = "iteration_planner:session:") -> None:
        try:
            from redis.asyncio import Redis  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - 取决于运行环境
            raise RuntimeError(
                "未安装可选依赖 redis，无法启用 SESSION_STORE=redis；请执行 `pip install redis` 或改回 memory"
            ) from exc
        self._redis = Redis.from_url(url, decode_responses=True)
        self._ttl = ttl_seconds
        self._prefix = prefix
        self._locks: dict[str, asyncio.Lock] = {}

    def _key(self, session_id: str) -> str:
        return f"{self._prefix}{session_id}"

    def _pending_key(self, session_id: str) -> str:
        return f"{self._prefix}{session_id}:pending"

    async def get(self, session_id: str) -> list[dict[str, Any]] | None:
        raw = await self._redis.get(self._key(session_id))
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        return data if isinstance(data, list) else None

    async def set(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        payload = json.dumps(messages, ensure_ascii=False, default=str)
        if self._ttl > 0:
            await self._redis.set(self._key(session_id), payload, ex=self._ttl)
        else:
            await self._redis.set(self._key(session_id), payload)

    async def get_pending(self, session_id: str) -> list[dict[str, Any]] | None:
        raw = await self._redis.get(self._pending_key(session_id))
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        return data if isinstance(data, list) else None

    async def set_pending(self, session_id: str, actions: list[dict[str, Any]]) -> None:
        key = self._pending_key(session_id)
        if not actions:
            await self._redis.delete(key)
            return
        payload = json.dumps(actions, ensure_ascii=False, default=str)
        if self._ttl > 0:
            await self._redis.set(key, payload, ex=self._ttl)
        else:
            await self._redis.set(key, payload)

    async def pop(self, session_id: str) -> bool:
        self._locks.pop(session_id, None)
        await self._redis.delete(self._pending_key(session_id))
        return bool(await self._redis.delete(self._key(session_id)))

    def lock(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    async def size(self) -> int:
        count = 0
        async for key in self._redis.scan_iter(match=f"{self._prefix}*", count=100):
            # 待确认动作使用 `<session>:pending` 后缀键，不计入会话数
            if not str(key).endswith(":pending"):
                count += 1
        return count

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self._redis.aclose()


def build_session_store(
    *,
    driver: str,
    max_sessions: int,
    ttl_seconds: int,
    redis_url: str,
) -> SessionStore:
    """按配置构建会话存储。"""
    if driver.lower() == "redis":
        if not redis_url:
            raise RuntimeError("SESSION_STORE=redis 但未配置 REDIS_URL")
        logger.info("会话存储：Redis（%s）", redis_url)
        return RedisSessionStore(redis_url, ttl_seconds=ttl_seconds)
    return InMemorySessionStore(max_sessions=max_sessions, ttl_seconds=ttl_seconds)


__all__ = ["SessionStore", "InMemorySessionStore", "RedisSessionStore", "build_session_store"]
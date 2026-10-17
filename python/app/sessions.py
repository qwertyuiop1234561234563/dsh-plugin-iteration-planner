"""会话存储（可插拔）。

Agent 的会话式对话需要跨请求保留上下文。企业级要求：

- **可插拔**：默认进程内存（带 TTL 与 LRU 上限），配置 ``REDIS_URL`` 后可切到 Redis
  （多副本 / 多 worker 共享会话）；
- **并发安全**：同一 ``session_id`` 的请求串行化（会话级锁），避免并发写入相互覆盖。
  内存实现用进程内 ``asyncio.Lock``；Redis 实现用 **Redis 租约锁**（``SET NX PX`` +
  Lua 校验 token 释放/续租），否则多副本下各进程各持有自己的锁，同一会话的并发请求
  会并发改写上下文并互相覆盖（last-write-wins，历史丢失）；
- **自清理**：内存实现提供后台过期清理任务，避免长跑进程内存泄漏。

Redis 为**可选依赖**：仅在 ``SESSION_STORE=redis`` 时惰性导入 ``redis``，未安装则给出
明确报错（不静默降级），避免「以为共享了会话其实各存各的」这类隐蔽故障；运行期 Redis
不可达时统一抛 ``SESSION_STORE_UNAVAILABLE``（503），不泄漏内部堆栈。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections import OrderedDict
from typing import Any, Protocol, runtime_checkable

from .errors import session_busy, session_store_unavailable

logger = logging.getLogger("iteration_planner.sessions")


@runtime_checkable
class SessionLock(Protocol):
    """会话级锁（``async with`` 语义；内存实现为 ``asyncio.Lock``，Redis 实现为分布式租约锁）。"""

    async def __aenter__(self) -> Any:
        ...

    async def __aexit__(self, *exc_info: object) -> Any:
        ...


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

    def lock(self, session_id: str) -> SessionLock:
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

    def __init__(
        self,
        *,
        max_sessions: int = 200,
        ttl_seconds: int = 3600,
        max_locks: int = 1024,
    ) -> None:
        self._max_sessions = max(1, max_sessions)
        self._ttl = ttl_seconds
        self._max_locks = max(16, max_locks)
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
        self._drop_lock(session_id)
        self._pending.pop(session_id, None)
        return self._sessions.pop(session_id, None) is not None

    def _drop_lock(self, session_id: str) -> None:
        """回收会话锁登记项——**仅在无人持有/等待时**。

        早期实现无条件 ``self._locks.pop(session_id)``：若此时该锁正被另一请求持有（或有人
        在等），下一次 :meth:`lock` 会为同一 session 新建**另一把**锁，两个请求随即同时进入
        临界区——互斥被静默打破，历史被并发覆盖（``DELETE /agent/sessions/{id}`` 与
        ``POST /agent/chat`` 并发时即可触发）。这里改为「锁空闲才回收」，空闲意味着既无持有者
        也无等待者（有等待者时 ``locked()`` 必为真）。
        """
        lock = self._locks.get(session_id)
        if lock is not None and not lock.locked():
            self._locks.pop(session_id, None)

    def lock(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            if len(self._locks) >= self._max_locks:
                # 有界回收：仅清理「未被持有」的条目，避免长跑进程 / 大量不同会话时
                # 锁登记表无界增长（Redis 实现已有同样的上限，此处对齐）
                for key in [sid for sid, item in self._locks.items() if not item.locked()]:
                    self._locks.pop(key, None)
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


class _RedisSessionLock:
    """会话级分布式租约锁（跨副本互斥；进程内先用 ``asyncio.Lock`` 走快速路径）。

    设计要点：

    - **令牌化**：锁值写入随机 token，释放与续租均通过 Lua **比较后**操作，
      避免「超时后锁被他人接管，自己却把别人的锁删掉」；
    - **租约续期**：持有期间按 ``TTL/3`` 后台续租，覆盖长耗时编排（多轮 LLM + 引擎调用）；
      续租失败（Redis 抖动）不中断业务，最坏情况锁按 TTL 自然过期；
    - **等待上限**：超过 ``wait_seconds`` 仍拿不到锁即抛 ``SESSION_BUSY``。明确失败优于
      「放弃互斥继续写」——后者会让同一会话的历史被并发覆盖。
    """

    #: 仅当值等于自己的 token 时才删除（防止误删他人锁）
    _RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
    #: 仅当值等于自己的 token 时才续期
    _RENEW = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('pexpire', KEYS[1], ARGV[2]) else return 0 end"

    def __init__(
        self,
        redis: Any,
        key: str,
        local: asyncio.Lock,
        *,
        ttl_seconds: float,
        wait_seconds: float,
        poll_interval: float = 0.05,
    ) -> None:
        self._redis = redis
        self._key = key
        self._local = local
        self._ttl_ms = max(1_000, int(ttl_seconds * 1000))
        self._wait = max(0.0, wait_seconds)
        self._poll = max(0.01, poll_interval)
        self._token: str | None = None
        self._renewer: asyncio.Task[None] | None = None

    async def __aenter__(self) -> "_RedisSessionLock":
        await self._local.acquire()
        try:
            await self._acquire_remote()
        except BaseException:
            # 拿不到远端锁必须释放本进程锁，否则同副本后续请求全部被本地锁挡住
            self._local.release()
            raise
        self._renewer = asyncio.create_task(self._renew_loop(), name="session-lock-renew")
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        if self._renewer is not None:
            self._renewer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._renewer
            self._renewer = None
        try:
            await self._release_remote()
        finally:
            self._local.release()

    async def _acquire_remote(self) -> None:
        token = uuid.uuid4().hex
        deadline = asyncio.get_running_loop().time() + self._wait
        while True:
            try:
                acquired = await self._redis.set(self._key, token, nx=True, px=self._ttl_ms)
            except Exception as exc:  # noqa: BLE001 - 统一映射为可重试的 503
                raise session_store_unavailable("会话存储（Redis）不可用：会话锁获取失败", str(exc)) from exc
            if acquired:
                self._token = token
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise session_busy("同一会话仍在处理上一条请求（跨副本锁未获取到），请稍后重试")
            await asyncio.sleep(self._poll)

    async def _release_remote(self) -> None:
        token, self._token = self._token, None
        if token is None:
            return
        with contextlib.suppress(Exception):
            await self._redis.eval(self._RELEASE, 1, self._key, token)

    async def _renew_loop(self) -> None:
        interval = max(1.0, self._ttl_ms / 3000.0)
        while True:
            await asyncio.sleep(interval)
            token = self._token
            if token is None:
                return
            try:
                renewed = await self._redis.eval(self._RENEW, 1, self._key, token, self._ttl_ms)
            except Exception:  # noqa: BLE001 - Redis 抖动不应中断业务，下一轮再试
                continue
            if not renewed:
                # 锁已过期或被他人接管：继续续租会掩盖冲突，交由 TTL 兜底并记录告警
                logger.warning("会话锁续租失败，锁可能已过期或被接管（key=%s）", self._key)
                self._token = None
                return


class RedisSessionStore:
    """Redis 会话存储（多副本共享；需安装可选依赖 ``redis`` 并配置 ``REDIS_URL``）。

    仅在 ``SESSION_STORE=redis`` 时启用；未安装 ``redis`` 时抛出明确错误，不静默降级。
    """

    def __init__(
        self,
        url: str,
        *,
        ttl_seconds: int = 3600,
        prefix: str = "iteration_planner:session:",
        lock_ttl_seconds: float = 300.0,
        lock_wait_seconds: float = 20.0,
        max_local_locks: int = 512,
    ) -> None:
        try:
            from redis.asyncio import Redis  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - 取决于运行环境
            raise RuntimeError(
                "未安装可选依赖 redis，无法启用 SESSION_STORE=redis；请执行 `pip install redis` 或改回 memory"
            ) from exc
        self._redis = Redis.from_url(url, decode_responses=True)
        self._ttl = ttl_seconds
        self._prefix = prefix
        self._lock_ttl = lock_ttl_seconds
        self._lock_wait = lock_wait_seconds
        self._max_local_locks = max(1, max_local_locks)
        self._locks: dict[str, asyncio.Lock] = {}

    def _key(self, session_id: str) -> str:
        return f"{self._prefix}{session_id}"

    def _pending_key(self, session_id: str) -> str:
        return f"{self._prefix}{session_id}:pending"

    def _lock_key(self, session_id: str) -> str:
        return f"{self._prefix}{session_id}:lock"

    async def _execute(self, awaitable: Any, description: str) -> Any:
        """执行 Redis 调用；把驱动层异常统一映射为可重试的 503（不泄漏内部堆栈）。"""
        try:
            return await awaitable
        except Exception as exc:  # noqa: BLE001 - 见上
            raise session_store_unavailable(f"会话存储（Redis）不可用：{description} 失败", str(exc)) from exc

    async def get(self, session_id: str) -> list[dict[str, Any]] | None:
        raw = await self._execute(self._redis.get(self._key(session_id)), "GET 会话历史")
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
            await self._execute(self._redis.set(self._key(session_id), payload, ex=self._ttl), "SET 会话历史")
        else:
            await self._execute(self._redis.set(self._key(session_id), payload), "SET 会话历史")

    async def get_pending(self, session_id: str) -> list[dict[str, Any]] | None:
        raw = await self._execute(self._redis.get(self._pending_key(session_id)), "GET 待确认动作")
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
            await self._execute(self._redis.delete(key), "DEL 待确认动作")
            return
        payload = json.dumps(actions, ensure_ascii=False, default=str)
        if self._ttl > 0:
            await self._execute(self._redis.set(key, payload, ex=self._ttl), "SET 待确认动作")
        else:
            await self._execute(self._redis.set(key, payload), "SET 待确认动作")

    async def pop(self, session_id: str) -> bool:
        self._locks.pop(session_id, None)
        await self._execute(self._redis.delete(self._pending_key(session_id)), "DEL 待确认动作")
        return bool(await self._execute(self._redis.delete(self._key(session_id)), "DEL 会话历史"))

    def lock(self, session_id: str) -> _RedisSessionLock:
        local = self._locks.get(session_id)
        if local is None:
            if len(self._locks) >= self._max_local_locks:
                # 有界回收：仅清理「未被持有」的条目，避免长跑进程的锁登记表无限增长
                for key in [sid for sid, item in self._locks.items() if not item.locked()]:
                    self._locks.pop(key, None)
            local = asyncio.Lock()
            self._locks[session_id] = local
        return _RedisSessionLock(
            self._redis,
            self._lock_key(session_id),
            local,
            ttl_seconds=self._lock_ttl,
            wait_seconds=self._lock_wait,
        )

    async def size(self) -> int:
        async def _scan() -> list[str]:
            keys: list[str] = []
            async for key in self._redis.scan_iter(match=f"{self._prefix}*", count=100):
                keys.append(str(key))
            return keys

        keys = await self._execute(_scan(), "SCAN 会话键")
        # 会话历史使用 `<session>` 主键；`:pending`（待确认动作）与 `:lock`（会话锁）
        # 均为附属键，不计入会话数
        return len([key for key in keys if not key.endswith((":pending", ":lock"))])

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
    lock_ttl_seconds: float = 300.0,
    lock_wait_seconds: float = 20.0,
) -> SessionStore:
    """按配置构建会话存储。"""
    if driver.lower() == "redis":
        if not redis_url:
            raise RuntimeError("SESSION_STORE=redis 但未配置 REDIS_URL")
        logger.info("会话存储：Redis（%s）", redis_url)
        return RedisSessionStore(
            redis_url,
            ttl_seconds=ttl_seconds,
            lock_ttl_seconds=lock_ttl_seconds,
            lock_wait_seconds=lock_wait_seconds,
        )
    return InMemorySessionStore(max_sessions=max_sessions, ttl_seconds=ttl_seconds)


__all__ = ["SessionStore", "SessionLock", "InMemorySessionStore", "RedisSessionStore", "build_session_store"]
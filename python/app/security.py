"""安全加固组件。

企业级对外网关需要的三件事，全部用标准库实现，不引入额外依赖：

1. **常量时间令牌比较**：避免 ``==`` 短路比较带来的时序侧信道（令牌逐字节猜测）；
2. **按客户端 IP 的令牌桶限流**：单机维度的过载保护，超限返回 ``429 RATE_LIMITED``；
3. **安全响应头**：把浏览器侧的 MIME 嗅探、点击劫持、Referer 泄露等常见风险默认关掉。

限流器默认单进程内存实现；``API_WORKERS > 1`` 或多副本部署时应设置 ``REDIS_URL``，
改用 :class:`RedisRateLimiter` 共享令牌桶——否则每个 worker / 副本各持一份配额，
实际全局配额约等于「配置值 × 副本数」，过载保护形同虚设（见 README）。
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from fastapi import Request

from .errors import payload_too_large
from .metrics import METRICS

logger = logging.getLogger("iteration_planner.security")

#: 默认注入的安全响应头（可在配置中关闭）
SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-site",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}


def constant_time_equals(provided: str, expected: str) -> bool:
    """常量时间比较两个字符串（长度不同也保持恒定开销）。

    直接使用 ``==`` 会在首个不同字节处提前返回，攻击者可通过响应时间差逐字节
    爆破令牌；``hmac.compare_digest`` 对全部字节做定长比较，消除该侧信道。
    """
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def parse_trusted_proxies(value: str | Iterable[str] | None) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """解析可信反代地址清单（``10.0.0.0/8,192.168.1.5`` 形式的逗号分隔字符串）。

    非法条目直接忽略（配置错误不应阻断服务启动，但会在日志中留下告警）。
    """
    if value is None:
        return ()
    items = value.split(",") if isinstance(value, str) else list(value)
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for item in items:
        candidate = str(item).strip()
        if not candidate:
            continue
        try:
            networks.append(ipaddress.ip_network(candidate, strict=False))
        except ValueError:
            logger.warning("忽略非法的可信反代配置项：%s", candidate)
    return tuple(networks)


def _peer_is_trusted(peer: str, trusted: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]) -> bool:
    if not trusted:
        return False
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(address in network for network in trusted)


def client_ip(request: Request, *, trusted_proxies: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = ()) -> str:
    """解析客户端标识（用于限流分桶）。

    ``X-Forwarded-For`` 是**客户端可伪造**的请求头：无条件信任它等于把限流分桶键
    交给攻击者（换一个 IP 就换一个桶，可无限绕过限流）。因此此处只在「直连对端
    命中配置的可信反代清单」时才采信该头，其余情况一律以 TCP 对端地址为准。

    部署在 Nginx / Ingress 之后时，须通过 ``API_TRUSTED_PROXIES`` 显式声明反代地址
    （如 ``10.0.0.0/8``），否则后端看到的统一是反代 IP，限流会退化为全站共享一个桶。
    """
    peer = request.client.host if request.client and request.client.host else ""
    if _peer_is_trusted(peer, trusted_proxies):
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            first = forwarded.split(",")[0].strip()
            if first:
                return first
    return peer or "unknown"


def _declared_length(scope: Any) -> int | None:
    """读取请求已声明的 ``content-length``（缺失 / 非法 / 负数 → ``None``）。"""
    for key, value in scope.get("headers") or ():
        if key.lower() == b"content-length":
            try:
                length = int(value)
            except (TypeError, ValueError):
                return None
            return length if length >= 0 else None
    return None


def _payload_too_large_body(max_bytes: int) -> bytes:
    return json.dumps(
        payload_too_large(f"请求体超过上限 {max_bytes} 字节", {"maxBodyBytes": max_bytes}).to_body(),
        ensure_ascii=False,
    ).encode("utf-8")


class _BodyTooLarge(Exception):
    """内部哨兵：请求体越界时由 ``limited_receive`` 抛出，仅供本中间件自己捕获。

    不能直接抛 :class:`app.errors.ApiError`：请求体的读取发生在 **FastAPI 异常处理层之外**
    （``BaseHTTPMiddleware`` 会在后台任务里监听客户端断连，顺带消费请求体；上游异常则直接
    穿过基类中间件的 ``collapse_excgroups``），因此既不会被 ``ExceptionMiddleware`` 的
    ``ApiError`` 处理器接住，也没法依赖它渲染 413 —— 实测会一路冒泡成未捕获异常。
    用独立的哨兵类型并在本中间件内捕获，语义最清晰、也不与业务 ``ApiError`` 混淆。
    """


def _carries_body_too_large(exc: BaseException) -> bool:
    """异常（或异常组）中是否包含请求体越界哨兵。

    必须递归判定：``BaseHTTPMiddleware`` 会在 anyio TaskGroup 内消费请求体（同时监听客户端
    断连），从 ``receive`` 抛出的哨兵会被 TaskGroup 包装成（可能多层嵌套的）
    ``BaseExceptionGroup``——普通 ``except _BodyTooLarge`` 捕获不到，结果就是本该回 413 的请求
    变成未捕获异常（500 + 连接中断 + 日志里一堆 Traceback）。
    """
    if isinstance(exc, _BodyTooLarge):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_carries_body_too_large(item) for item in exc.exceptions)
    return False


class BodySizeLimitMiddleware:
    """请求体大小上限（纯 ASGI 实现）。

    动机：Starlette / FastAPI **默认不限制请求体大小**。上传与 JSON 端点都会把 body 整体
    读进内存，一个超大请求即可显著抬高内存并挤占其余请求——而限流按「请求数/分钟」计量，
    完全防不住这一维度。

    为什么不用 ``BaseHTTPMiddleware``：``call_next`` 会把请求体读完再交给下游，中间件
    本身没有机会在「读到一半」时中断；只有直接包装 ``receive``，才能同时拦住两种写法：

    - **已声明** ``content-length`` 且超限：不读 body，``O(1)`` 直接回 413；
    - **未声明长度**（``Transfer-Encoding: chunked``，或声明值小于实际）：逐块累计，
      一旦越界立即抛 :class:`_BodyTooLarge` **中断请求体读取**，并由本中间件就地回 413；
      超出的部分**不会**被读进内存。

    413 响应仍经原 ``send`` 下发，因此外层中间件（安全首部 / 访问日志 / 请求指标）照常生效。
    ``http.response.start`` 若已下发则无法补发 413，此时只记录并向上抛出（连接会被关闭）。

    :param max_bytes: 上限（字节）；``<= 0`` 表示关闭该限制。
    :param exempt_prefixes: 豁免的路径前缀（如内部健康探测端点）。
    """

    def __init__(self, app: Any, *, max_bytes: int, exempt_prefixes: Iterable[str] = ()) -> None:
        self._app = app
        self._max_bytes = int(max_bytes)
        self._exempt_prefixes = tuple(exempt_prefixes)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or self._max_bytes <= 0 or self._is_exempt(scope):
            await self._app(scope, receive, send)
            return

        declared = _declared_length(scope)
        if declared is not None and declared > self._max_bytes:
            METRICS.inc_counter("http_body_rejected_total", {"source": "content_length"})
            await self._send_rejection(send)
            return

        received = 0
        started = False

        async def limited_receive() -> Any:
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body") or b"")
                if received > self._max_bytes:
                    METRICS.inc_counter("http_body_rejected_total", {"source": "chunked"})
                    raise _BodyTooLarge
            return message

        async def guarded_send(message: Any) -> None:
            nonlocal started
            if message.get("type") == "http.response.start":
                started = True
            await send(message)

        try:
            await self._app(scope, limited_receive, guarded_send)
        except (_BodyTooLarge, BaseExceptionGroup) as exc:
            if not _carries_body_too_large(exc):
                raise
            if started:  # 已开始响应，无法再补发 413；交由上层关闭连接
                logger.warning("请求体越界但响应已开始，无法回 413（path=%s）", scope.get("path"))
                raise
            await self._send_rejection(send)

    async def _send_rejection(self, send: Any) -> None:
        body = _payload_too_large_body(self._max_bytes)
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    def _is_exempt(self, scope: Any) -> bool:
        if not self._exempt_prefixes:
            return False
        return str(scope.get("path") or "").startswith(self._exempt_prefixes)


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


class RateLimiter:
    """令牌桶限流器（按 key 分桶，线程 / 协程安全地惰性补充令牌）。

    :param per_minute: 每分钟允许的请求数（桶容量与补充速率）；``<=0`` 表示关闭。
    :param burst: 桶容量（允许的瞬时突发）；缺省与 ``per_minute`` 相同。
    """

    def __init__(self, per_minute: int, burst: int | None = None) -> None:
        self._enabled = per_minute > 0
        self._capacity = float(burst if burst and burst > 0 else per_minute)
        self._refill_per_sec = per_minute / 60.0
        self._buckets: dict[str, _Bucket] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    def allow(self, key: str) -> tuple[bool, int]:
        """判断是否放行。

        :return: ``(是否放行, 建议重试等待秒数)``；放行时等待秒数为 0。
        """
        if not self._enabled:
            return True, 0

        now = time.monotonic()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=self._capacity, updated_at=now)
            self._buckets[key] = bucket

        # 惰性补充：按经过时间补充令牌，上限为桶容量
        elapsed = max(0.0, now - bucket.updated_at)
        bucket.tokens = min(self._capacity, bucket.tokens + elapsed * self._refill_per_sec)
        bucket.updated_at = now

        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return True, 0

        deficit = 1.0 - bucket.tokens
        retry_after = max(1, int(deficit / self._refill_per_sec) if self._refill_per_sec > 0 else 1)
        return False, retry_after

    def prune(self, idle_seconds: float = 600.0) -> int:
        """清理长时间未活跃的桶，避免内存随客户端数量无界增长。"""
        if not self._enabled:
            return 0
        now = time.monotonic()
        stale = [key for key, bucket in self._buckets.items() if now - bucket.updated_at > idle_seconds]
        for key in stale:
            self._buckets.pop(key, None)
        return len(stale)


# ------------------------------- 限流后端抽象 -------------------------------


@runtime_checkable
class RateLimitBackend(Protocol):
    """限流后端接口（异步，便于经 Redis 跨副本共享）。"""

    @property
    def name(self) -> str:
        """后端标识（memory / redis），用于日志与健康检查。"""
        ...

    @property
    def enabled(self) -> bool:
        """是否启用限流（``API_RATE_LIMIT_PER_MINUTE <= 0`` 时为 ``False``）。"""
        ...

    async def allow(self, key: str) -> tuple[bool, int]:
        """判断是否放行，返回 ``(是否放行, 建议重试等待秒数)``。"""
        ...

    async def close(self) -> None:
        """释放后端持有的连接 / 资源（Redis 后端在应用关停时关闭连接池）。"""
        ...


class MemoryRateLimitBackend:
    """进程内存令牌桶的异步适配（复用 :class:`RateLimiter`，单进程部署默认）。"""

    def __init__(self, per_minute: int, burst: int | None = None) -> None:
        self._limiter = RateLimiter(per_minute, burst)

    @property
    def name(self) -> str:
        return "memory"

    @property
    def enabled(self) -> bool:
        return self._limiter.enabled

    async def allow(self, key: str) -> tuple[bool, int]:
        return self._limiter.allow(key)

    async def close(self) -> None:
        return None

    def prune(self, idle_seconds: float = 600.0) -> int:
        return self._limiter.prune(idle_seconds)


class RedisRateLimiter:
    """Redis 共享令牌桶（跨 worker / 跨副本的**全局**配额）。

    动机：进程内限流在每个 worker 各持一份配额，``API_WORKERS > 1`` 时实际全局配额
    约等于「配置值 × worker 数」，过载保护被稀释。改用 Redis 后，配额在副本间共享。

    实现：令牌桶状态存 Redis Hash（``tokens`` / ``ts``），**判定与扣减在同一段 Lua 内原子完成**
    （否则「先读后写」会在并发下超发）；键设置 TTL，空闲桶自动过期，不产生无界键增长。

    故障语义为 **fail-open**：Redis 不可达时放行并计数告警。理由——限流是可用性保护，
    不应在存储抖动时把所有正常用户一起拒之门外；但会暴露 ``rate_limit_backend_errors_total``
    指标，便于告警发现「限流实际已失效」。
    """

    #: 原子令牌桶：ARGV = [容量, 每秒补充, 当前秒, 键TTL毫秒]；返回 {是否放行, 建议等待秒数}
    _SCRIPT = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])
local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil or ts == nil then
  tokens = capacity
  ts = now
end
local elapsed = now - ts
if elapsed > 0 then
  tokens = math.min(capacity, tokens + elapsed * refill)
end
local allowed = 0
local retry = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
elseif refill > 0 then
  retry = math.ceil((1 - tokens) / refill)
end
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', key, ttl)
return {allowed, retry}
"""

    def __init__(
        self,
        redis: Any,
        *,
        per_minute: int,
        burst: int | None = None,
        prefix: str = "iteration_planner:ratelimit:",
        error_log_interval: float = 30.0,
    ) -> None:
        self._redis = redis
        self._enabled = per_minute > 0
        self._capacity = float(burst if burst and burst > 0 else per_minute)
        self._refill_per_sec = per_minute / 60.0
        self._prefix = prefix
        # 空闲桶存活时间：容量 ÷ 补充速率 × 余量，保证「重新活跃时桶已补满」的前提下尽量短
        self._key_ttl_ms = max(60_000, int(self._capacity / max(self._refill_per_sec, 1e-9) * 2 * 1000))
        self._error_log_interval = max(1.0, error_log_interval)
        self._last_error_log_at = 0.0
        self._suppressed_errors = 0

    @property
    def name(self) -> str:
        return "redis"

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def allow(self, key: str) -> tuple[bool, int]:
        if not self._enabled:
            return True, 0
        # 限流键可能含 IP 等可识别信息，哈希后再落到 Redis，避免明文外泄与键过长
        bucket_key = self._prefix + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        try:
            result = await self._redis.eval(
                self._SCRIPT,
                1,
                bucket_key,
                self._capacity,
                self._refill_per_sec,
                time.time(),
                self._key_ttl_ms,
            )
            allowed = int(result[0]) if isinstance(result, (list, tuple)) else int(result)
            retry_after = int(result[1]) if isinstance(result, (list, tuple)) and len(result) > 1 else 0
            return bool(allowed), max(0, min(retry_after, 3600))
        except Exception as exc:  # noqa: BLE001 - fail-open：限流后端故障不应拒绝正常流量
            self._record_error(exc)
            return True, 0

    async def close(self) -> None:
        """应用关停时释放 Redis 连接池（避免连接泄漏 / 未关闭告警）。"""
        with contextlib.suppress(Exception):
            await self._redis.aclose()

    def _record_error(self, exc: BaseException) -> None:
        METRICS.inc_counter(
            "rate_limit_backend_errors_total",
            {"backend": "redis"},
            help="限流后端故障次数（fail-open 计数，>0 说明限流实际已失效）",
        )
        now = time.monotonic()
        if now - self._last_error_log_at < self._error_log_interval:
            self._suppressed_errors += 1
            return
        suppressed, self._suppressed_errors = self._suppressed_errors, 0
        self._last_error_log_at = now
        logger.warning(
            "限流后端（Redis）异常，已降级为放行（近 %.0fs 内另有 %d 次同类错误）：%s",
            self._error_log_interval,
            suppressed,
            exc,
        )


def build_rate_limiter(
    *,
    per_minute: int,
    burst: int | None = None,
    redis_url: str = "",
    driver: str = "auto",
) -> RateLimitBackend:
    """按配置构建限流后端。

    :param driver: ``auto``（有 ``REDIS_URL`` 且依赖可用则用 Redis）| ``memory`` | ``redis``
    :raises RuntimeError: 显式选择 ``redis`` 但缺少 ``REDIS_URL`` 或未安装依赖
        （显式选择时静默降级会让「以为全局限流了其实没有」的隐患不可见）
    """
    choice = (driver or "auto").strip().lower()
    use_redis = choice == "redis" or (choice == "auto" and bool(redis_url))
    if not use_redis:
        if per_minute > 0:
            logger.info("限流后端：进程内存（每客户端 %d 次/分钟，单进程生效）", per_minute)
        return MemoryRateLimitBackend(per_minute, burst)
    if not redis_url:
        raise RuntimeError("RATE_LIMIT_BACKEND=redis 但未配置 REDIS_URL")
    try:
        from redis.asyncio import Redis  # type: ignore[import-not-found]
    except ImportError as exc:
        if choice == "redis":
            raise RuntimeError(
                "未安装可选依赖 redis，无法启用 RATE_LIMIT_BACKEND=redis；请执行 `pip install redis` 或改用 memory"
            ) from exc
        logger.warning("未安装可选依赖 redis，限流自动降级为进程内存")
        return MemoryRateLimitBackend(per_minute, burst)

    client = Redis.from_url(
        redis_url,
        decode_responses=True,
        socket_timeout=1.0,
        socket_connect_timeout=1.0,
    )
    if per_minute > 0:
        logger.info("限流后端：Redis（全局配额 %d 次/分钟，多副本共享）", per_minute)
    else:
        logger.info("限流后端：Redis（限流未启用，API_RATE_LIMIT_PER_MINUTE=0）")
    return RedisRateLimiter(client, per_minute=per_minute, burst=burst)


__all__ = [
    "SECURITY_HEADERS",
    "BodySizeLimitMiddleware",
    "RateLimiter",
    "MemoryRateLimitBackend",
    "RedisRateLimiter",
    "RateLimitBackend",
    "build_rate_limiter",
    "client_ip",
    "constant_time_equals",
    "parse_trusted_proxies",
]
"""安全加固组件。

企业级对外网关需要的三件事，全部用标准库实现，不引入额外依赖：

1. **常量时间令牌比较**：避免 ``==`` 短路比较带来的时序侧信道（令牌逐字节猜测）；
2. **按客户端 IP 的令牌桶限流**：单机维度的过载保护，超限返回 ``429 RATE_LIMITED``；
3. **安全响应头**：把浏览器侧的 MIME 嗅探、点击劫持、Referer 泄露等常见风险默认关掉。

限流器为单进程内存实现；多副本部署时应由网关 / Redis 承担全局限流（见 README）。
"""

from __future__ import annotations

import hmac
import time
from dataclasses import dataclass

from fastapi import Request

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


def client_ip(request: Request) -> str:
    """解析客户端标识（用于限流分桶）。

    仅信任直连来源（``request.client``）；若服务部署在可信反代之后，可由反代
    覆写 ``X-Forwarded-For``，此处保守地取最左侧地址作为兜底。
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


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
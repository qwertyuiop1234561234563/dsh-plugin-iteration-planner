"""韧性组件：断路器 + 重试策略。

用于「Python 网关 → 内部算法引擎」这一跳，防止引擎抖动把对外服务一起拖垮。

- **断路器**：连续失败达到阈值后开路，冷却期内直接快速失败（``503 ENGINE_CIRCUIT_OPEN``），
  冷却结束进入半开态放行探测请求，成功则恢复闭合。
- **重试**：仅对**幂等 / 连接阶段失败**的请求重试（连接失败意味着请求未送达，任何方法都安全；
  读超时 / 5xx 只对幂等方法重试），避免对 ``POST /plans`` 这类非幂等写操作产生重复副作用。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum

import httpx

logger = logging.getLogger("iteration_planner.resilience")

#: 幂等 HTTP 方法（重试安全）
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})

#: 连接阶段异常：请求尚未送达上游，重试不会造成重复副作用
CONNECT_PHASE_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

#: 可重试的上游状态码（服务端暂不可用）
RETRYABLE_STATUS = frozenset({502, 503, 504})


class CircuitState(str, Enum):
    """断路器状态。"""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """三态断路器（单进程内存实现）。"""

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 1,
    ) -> None:
        self._failure_threshold = max(1, failure_threshold)
        self._recovery_timeout = recovery_timeout
        self._half_open_max_calls = max(1, half_open_max_calls)
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._half_open_calls = 0

    @property
    def state(self) -> CircuitState:
        # 读取时惰性推进 OPEN → HALF_OPEN，保证 /metrics 与健康检查反映真实状态
        self._maybe_half_open()
        return self._state

    def allow(self) -> bool:
        """是否放行本次调用。"""
        self._maybe_half_open()
        if self._state is CircuitState.OPEN:
            return False
        if self._state is CircuitState.HALF_OPEN:
            if self._half_open_calls >= self._half_open_max_calls:
                return False
            self._half_open_calls += 1
        return True

    def record_success(self) -> None:
        if self._state is not CircuitState.CLOSED:
            logger.info("引擎断路器恢复闭合（原状态=%s）", self._state.value)
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._half_open_calls = 0

    def record_failure(self) -> None:
        if self._state is CircuitState.HALF_OPEN:
            self._trip()
            return
        self._failures += 1
        if self._failures >= self._failure_threshold:
            self._trip()

    def _trip(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = time.monotonic()
        self._half_open_calls = 0
        logger.warning("引擎断路器开路：连续失败达到阈值 %d，%.0fs 内快速失败", self._failure_threshold, self._recovery_timeout)

    def _maybe_half_open(self) -> None:
        if self._state is CircuitState.OPEN and time.monotonic() - self._opened_at >= self._recovery_timeout:
            self._state = CircuitState.HALF_OPEN
            self._half_open_calls = 0


@dataclass(frozen=True)
class RetryPolicy:
    """指数退避重试策略。"""

    attempts: int = 2  #: 额外重试次数（0 表示不重试）
    base_delay: float = 0.2
    max_delay: float = 2.0

    def delay_for(self, retry_index: int) -> float:
        """第 ``retry_index`` 次重试（0 起）的等待秒数。"""
        return min(self.max_delay, self.base_delay * (2 ** retry_index))


def is_retryable_exception(exc: BaseException, method: str) -> bool:
    """异常是否可重试：连接阶段失败一律可重试；读超时仅幂等方法可重试。"""
    if isinstance(exc, CONNECT_PHASE_ERRORS):
        return True
    if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout)):
        return method.upper() in IDEMPOTENT_METHODS
    return False


def is_retryable_status(status_code: int, method: str) -> bool:
    """上游状态码是否可重试：仅幂等方法重试 5xx 网关类错误。"""
    return status_code in RETRYABLE_STATUS and method.upper() in IDEMPOTENT_METHODS


__all__ = [
    "CircuitBreaker",
    "CircuitState",
    "RetryPolicy",
    "is_retryable_exception",
    "is_retryable_status",
    "IDEMPOTENT_METHODS",
]
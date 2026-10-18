"""幂等键（``Idempotency-Key``）：让「重试」不再变成「重复写」。

背景：长耗时写最危险的失败模式不是「失败」，而是**客户端不知道到底成没成功**——
网络超时、网关 504、用户手抖重试，都会让同一次业务意图被提交多次。对建计划这类写
接口，重复提交意味着引擎里多出一份互相矛盾的需求计划，而且两份都"合法"。

做法对齐 Stripe 与 IETF ``draft-ietf-httpapi-idempotency-key-header-07``：

    首次请求   → 原子抢占键 → 执行 → 落「已完成 + 上次响应」
    同键重放   → 指纹一致且已完成 → **原样回放上次响应**（绝不重复执行）
    同键处理中 → ``409`` + ``Retry-After``（同一业务意图不并发执行）
    同键异载荷 → ``422``（键被复用到不同请求上，属调用方 bug，必须显式报错）

作用域：键按「主体 + 首部值」拼接，避免不同用户碰巧用同一个键而互相串写。

降级：幂等是**保护措施**而不是事实来源，因此存储故障时 **fail-open**——放行请求并在
响应首部标注 ``Idempotency-Status: skipped``。让保护措施本身变成新的可用性缺口，是
比「少一层保护」更糟的取舍。

与 :mod:`app.jobs` 的分工：作业解决「一次请求跑太久」，幂等解决「同一个意图提交多次」；
两者叠加后，客户端即便超时重试，也只会拿到**同一个作业**。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from .constants import IDEMPOTENCY_KEY_MAX_LEN
from .errors import bad_request, idempotency_conflict, idempotency_mismatch
from .kvstore import KvStoreError, build_kv_store
from .metrics import METRICS

logger = logging.getLogger("iteration_planner.idempotency")

#: 幂等键前缀（与作业 / 缓存前缀区分，便于运维按前缀清理）
IDEMPOTENCY_PREFIX = "iteration_planner:idempotency:"

#: 记录状态：处理中 / 已完成
_STATE_IN_PROGRESS = "in_progress"
_STATE_COMPLETED = "completed"

#: 判定结果
DECISION_PROCEED = "proceed"  #: 首次请求，已占位，可执行
DECISION_REPLAY = "replay"  #: 已完成，回放上次响应
DECISION_CONFLICT = "conflict"  #: 同键请求仍在处理中
DECISION_MISMATCH = "mismatch"  #: 同键但载荷不同
DECISION_DEGRADED = "degraded"  #: 存储不可用，放弃幂等保护（放行）

#: 需要幂等保护的方法（其余方法天然幂等，加保护只会白占存储）
_IDEMPOTENT_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: 回放时需要保留的响应首部：只保留下游真正依赖的少数几个
#: （``Location`` 指向作业资源、``Prefer`` 表明异步受理；其余逐跳首部不应跨请求复用）
_REPLAY_HEADERS = ("location", "prefer", "content-type")


@dataclass(frozen=True)
class Decision:
    """幂等判定结果（``REPLAY`` 时携带上次响应快照）。"""

    kind: str
    response: dict[str, Any] | None = None


def is_idempotent_method(method: str) -> bool:
    return (method or "").upper() in _IDEMPOTENT_METHODS


def fingerprint(method: str, path: str, params: list[tuple[str, str]], body: bytes) -> str:
    """请求指纹：方法 + 路径 + 查询串 + 请求体。

    不含主体：键本身已按主体作用域隔离（见 :func:`scoped_key`），再拼一遍只会让
    日志里的指纹失去「两次请求是否同一意图」的直观可比性。
    """
    digest = hashlib.sha256()
    digest.update((method or "").upper().encode("utf-8"))
    digest.update(b"\x00")
    digest.update(path.encode("utf-8"))
    digest.update(b"\x00")
    # 查询参数先排序再拼接：同一组参数的不同书写顺序应视为同一意图
    for key, value in sorted(params):
        digest.update(key.encode("utf-8"))
        digest.update(b"=")
        digest.update(value.encode("utf-8"))
        digest.update(b"\x00")
    digest.update(b"\x00")
    digest.update(body or b"")
    return digest.hexdigest()


def scoped_key(owner: str, raw_key: str) -> str:
    """按主体作用域拼接存储键（不同用户用同一个键不会互相串写）。"""
    return f"{owner}:{raw_key}"


class IdempotencyGuard:
    """幂等键守卫：判定 / 记录 / 回放。

    记录读写统一落在 :mod:`app.kvstore`：进程内存（单副本）或 Redis（多副本共享）。
    多副本部署必须用 Redis，否则各副本各存各的键，同一意图仍会被执行多次。
    """

    def __init__(self, kv, *, ttl_seconds: float = 86400.0) -> None:
        self._kv = kv
        self._ttl = ttl_seconds

    @property
    def backend(self) -> str:
        return self._kv.name

    def describe(self) -> dict[str, Any]:
        return {"ttlSeconds": self._ttl, **self._kv.describe()}

    async def close(self) -> None:
        """释放底层连接（优雅关机时调用；与其它可插拔组件保持同一生命周期接口）。"""
        await self._kv.close()

    async def decide(self, key: str, request_fingerprint: str) -> Decision:
        """判定本次请求应如何处置。

        **抢占必须原子**（``set_nx``）：多副本并发下若用「先读再写」，两个副本会同时
        通过读检查，又同时写入，同一意图照样被执行两次。
        """
        try:
            claimed = await self._kv.set_nx(
                key,
                {"fingerprint": request_fingerprint, "state": _STATE_IN_PROGRESS},
                self._ttl,
            )
        except KvStoreError as exc:
            return self._degraded("set_nx", exc, key)

        if claimed:
            return Decision(DECISION_PROCEED)

        try:
            existing = await self._kv.get(key)
        except KvStoreError as exc:
            return self._degraded("get", exc, key)

        if not isinstance(existing, dict):
            # 抢占失败却读不到记录：说明它刚好过期（或脏数据）。按「首次请求」处理，
            # 因为此时没有任何已完成的结果可供回放。
            return Decision(DECISION_PROCEED)

        if existing.get("fingerprint") != request_fingerprint:
            return Decision(DECISION_MISMATCH)
        if existing.get("state") == _STATE_COMPLETED:
            return Decision(DECISION_REPLAY, response=existing.get("response"))
        return Decision(DECISION_CONFLICT)

    async def finish(self, key: str, response: dict[str, Any]) -> None:
        """把键标记为已完成并记录响应快照（供后续同键请求回放）。"""
        try:
            await self._kv.set(
                key,
                {"fingerprint": response["fingerprint"], "state": _STATE_COMPLETED, "response": response},
                self._ttl,
            )
        except KvStoreError as exc:  # 写入失败不影响本次响应正确性，仅失去后续回放能力
            self._degraded("finish", exc, key)

    async def release(self, key: str) -> None:
        """释放占位（本次执行失败时调用）。

        失败（4xx / 5xx）的响应没有回放价值，甚至会让客户端误以为「同样的错误会稳定
        复现」；释放后同一个键可以重试，符合 RFC 草稿「请求失败则键可复用」的语义。
        """
        try:
            await self._kv.delete(key)
        except KvStoreError as exc:
            self._degraded("release", exc, key)

    def _degraded(self, operation: str, exc: BaseException, key: str = "") -> Decision:
        logger.warning("幂等存储不可用（%s），本次请求跳过幂等保护：%s", operation, exc)
        METRICS.inc_counter(
            "idempotency_total",
            {"outcome": "degraded", "operation": operation},
            help="幂等键处置次数（按结果与操作分标签）",
        )
        # 单列存储错误计数器：``idempotency_total`` 混了正常结果，运维按它告警会被噪声淹没；
        # 这条只随「存储真的坏了」增长，是唯一适合直接配告警的序列。
        METRICS.inc_counter(
            "idempotency_store_errors_total",
            {"operation": operation},
            help="幂等存储不可用次数（按操作分标签）",
        )
        _audit("idempotency.degraded", key=key, operation=operation, error=exc.__class__.__name__)
        return Decision(DECISION_DEGRADED)


def build_idempotency_guard(
    *,
    driver: str = "auto",
    redis_url: str = "",
    max_entries: int = 4096,
    ttl_seconds: float = 86400.0,
):
    """构建幂等守卫；后端选择与作业存储共用同一套驱动语义。

    ``driver=redis`` 但缺少 ``REDIS_URL`` 或未安装 ``redis`` 依赖时**启动即失败**：
    多副本下静默退化成「各副本各存各的幂等键」等于幂等保护形同虚设，比直接报错更危险。
    """
    kv = build_kv_store(
        store="idempotency",
        driver=driver,
        redis_url=redis_url,
        max_entries=max_entries,
        prefix=IDEMPOTENCY_PREFIX,
    )
    return IdempotencyGuard(kv, ttl_seconds=ttl_seconds)


async def run_idempotent(
    guard,
    *,
    owner: str,
    method: str,
    path: str,
    params: list[tuple[str, str]],
    body: bytes,
    raw_key: str,
    producer: Callable[[], Any],
    on_replay=None,
    on_degraded=None,
    on_proceed=None,
):
    """幂等化的请求执行外壳。

    :param producer: 无协程参数的 awaitable 工厂，返回 ``Response``。
    :param on_replay/on_degraded: 可选的响应加工钩子（用于加 ``Idempotency-Status`` 首部）。
    """
    if guard is None or not raw_key:
        return await producer()

    started = time.monotonic()
    key = scoped_key(owner, raw_key)
    digest = fingerprint(method, path, params, body)
    decision = await guard.decide(key, digest)

    def _elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    if decision.kind == DECISION_MISMATCH:
        _count("mismatch")
        _audit("idempotency.mismatch", key=key, user=owner, method=method, path=path, durationMs=_elapsed_ms())
        raise idempotency_mismatch()
    if decision.kind == DECISION_CONFLICT:
        _count("conflict")
        _audit("idempotency.conflict", key=key, user=owner, method=method, path=path, durationMs=_elapsed_ms())
        raise idempotency_conflict()
    if decision.kind == DECISION_DEGRADED:
        # 存储不可用时「本次放行了」，调用方拿到 ``Idempotency-Status: skipped``；
        # 审计里记一笔，运维才能把「重复写」的现场与存储故障对上号。
        _audit("idempotency.skipped", key=key, user=owner, method=method, path=path)
        response = await producer()
        return on_degraded(response) if on_degraded else response
    if decision.kind == DECISION_REPLAY:
        _count("replayed")
        _audit("idempotency.replayed", key=key, user=owner, method=method, path=path, durationMs=_elapsed_ms())
        response = _replay_response(decision.response)
        return on_replay(response) if on_replay else response

    response = await producer()
    if response.status_code < 400:
        await guard.finish(key, {**_capture(response), "fingerprint": digest})
        _count("stored")
        _audit("idempotency.stored", key=key, user=owner, method=method, path=path, durationMs=_elapsed_ms())
    else:
        # 失败响应不回放：释放占位，让调用方能用同一个键重试
        await guard.release(key)
        _count("released")
        _audit("idempotency.released", key=key, user=owner, method=method, path=path, durationMs=_elapsed_ms())
    return response


def validate_key(raw_key: str) -> None:
    """校验幂等键长度。

    :raises ApiError: 超过长度上限（400）。过长的键会被当作存储滥用面。
    """
    if len(raw_key) > IDEMPOTENCY_KEY_MAX_LEN:
        raise bad_request(
            f"Idempotency-Key 超过长度上限（{IDEMPOTENCY_KEY_MAX_LEN} 字符）",
            details=[{"field": "Idempotency-Key", "message": "键过长"}],
        )


def _capture(response) -> dict[str, Any]:
    """把响应快照成可 JSON 序列化的记录（响应体 base64，二进制安全）。"""
    body = getattr(response, "body", None)
    headers = {
        name: response.headers[name]
        for name in _REPLAY_HEADERS
        if response.headers.get(name) is not None
    }
    return {
        "status": response.status_code,
        "body": base64.b64encode(bytes(body)).decode("ascii") if isinstance(body, (bytes, bytearray)) else "",
        "headers": headers,
    }


def _replay_response(record: dict[str, Any] | None):
    """由记录重建响应（未知 / 不完整记录时退化为「无内容」响应）。"""
    from fastapi.responses import Response

    if not isinstance(record, dict):
        return Response(status_code=204)
    try:
        status = int(record.get("status") or 204)
    except (TypeError, ValueError):
        status = 204
    raw = record.get("body") or ""
    try:
        content = base64.b64decode(raw) if raw else b""
    except (ValueError, TypeError):
        content = b""
    headers = {
        key: value
        for key, value in (record.get("headers") or {}).items()
        if key.lower() != "content-type"
    }
    media_type = (record.get("headers") or {}).get("content-type")
    if not content:
        return Response(status_code=status, headers=headers or None)
    return Response(content=content, status_code=status, headers=headers or None, media_type=media_type)


def _count(outcome: str) -> None:
    METRICS.inc_counter("idempotency_total", {"outcome": outcome}, help="幂等键处置次数（按结果与操作分标签）")


def _audit(event: str, **fields: Any) -> None:
    """幂等处置审计日志（结构化字段，与作业审计同款，便于按 key / 用户 / 请求号检索）。"""
    from .logging_config import request_id_var

    logger.info(
        "幂等审计：%s",
        event,
        extra={"extra_fields": {"audit": event, "requestId": request_id_var.get() or "", **fields}},
    )


__all__ = [
    "IDEMPOTENCY_PREFIX",
    "DECISION_CONFLICT",
    "DECISION_DEGRADED",
    "DECISION_MISMATCH",
    "DECISION_PROCEED",
    "DECISION_REPLAY",
    "Decision",
    "IdempotencyGuard",
    "build_idempotency_guard",
    "fingerprint",
    "is_idempotent_method",
    "run_idempotent",
    "scoped_key",
    "validate_key",
]

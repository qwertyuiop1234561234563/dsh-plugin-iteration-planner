"""异步作业：长耗时写的「状态资源」。

为什么需要它：建计划 / 重规划在引擎侧是**同步阻塞**的算法计算（实测 80s+）。让客户端
的连接一直挂到计算结束，会在链路上留下两个必然故障点：

1. 任何中间层（浏览器 axios / 网关 / nginx / LB）的超时都早于引擎返回，于是客户端看到
   失败、而引擎其实已经落库 —— 产生「报失败但已写入」的**孤儿写**；
2. 超时后客户端无从得知上游最终结果，只能盲目重试，是否重复落库全靠运气。

因此采用业界通行范式（Google LRO / Dify / Airflow / Temporal 同构）：

    POST /api/plans  +  Prefer: respond-async   →  202 + {jobId, pollUrl, streamUrl}
    GET  /api/jobs/{id}                          →  作业快照（状态 / 进度 / 结果）
    GET  /api/jobs/{id}/stream                   →  SSE 进度流（progress → done）

**为什么不给引擎加作业 API**：算法引擎（``src/**``）是算法单一事实来源且被约束为不可
修改；作业语义可在网关层以「后台任务 + 状态资源」等价实现，代价是状态需要共享存储
（``REDIS_URL``）才能在多副本下可见。

取消语义（重要，已如实告知）：引擎侧同步处理器**无法被网关单方面中断**，因此取消只
标记作业终态并尽力取消本地任务；若取消到达时引擎已完成计算，作业会记录真实结果并
把状态置为 ``cancelled``，进度文案里说明「结果已记录」，绝不谎称「未执行」。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .constants import (
    JOB_MAX_PROGRESS_ITEMS,
    JOB_MAX_REQUEST_BYTES,
    JOB_MAX_RESULT_BYTES,
    JOB_PHASE_ENGINE,
    JOB_PHASE_QUEUED,
    JOB_PHASE_SETTLED,
    JOB_STATUS_CANCELLED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    JOB_TERMINAL_STATUSES,
)
from .errors import ApiError, job_conflict, job_not_found, job_store_unavailable, payload_too_large
from .kvstore import KvStoreError, build_kv_store
from .metrics import METRICS

logger = logging.getLogger("iteration_planner.jobs")

#: 作业键前缀（与 session / cache 前缀区分，便于运维按前缀清理与观测）
JOB_PREFIX = "iteration_planner:job:"

#: 作业耗时分桶（秒）：普通接口的默认分桶上限只有 10s，长耗时作业全部落进 ``+Inf``，
#: 观测不到分布，因此为作业单独给一套覆盖到 10 分钟的分桶。
JOB_DURATION_BUCKETS: tuple[float, ...] = (0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0, 300.0, 600.0)


@dataclass
class JobRecord:
    """作业记录（整体 JSON 序列化进键值存储）。"""

    jobId: str
    kind: str
    owner: str
    method: str
    path: str
    status: str = JOB_STATUS_PENDING
    planId: str | None = None
    createdAt: float = field(default_factory=time.time)
    updatedAt: float = field(default_factory=time.time)
    #: 转发给引擎的查询参数（``[[key, value], ...]``，保持多值参数的原始顺序）
    params: list[list[str]] = field(default_factory=list)
    #: 请求体（base64）：二进制安全，且 JSON 可序列化
    body: str | None = None
    #: 转发给引擎的请求首部（仅 content-type / accept 等白名单）
    headers: dict[str, str] = field(default_factory=dict)
    #: 进度轨迹（最近 ``JOB_MAX_PROGRESS_ITEMS`` 条）
    progress: list[dict[str, Any]] = field(default_factory=list)
    #: 成功结果：``{"statusCode", "planId", "data"}``（``data`` 过大时省略）
    result: dict[str, Any] | None = None
    #: 失败信息：仅稳定错误码 + 通用文案（绝不落栈 / 上游细节）
    error: dict[str, str] | None = None

    @property
    def terminal(self) -> bool:
        return self.status in JOB_TERMINAL_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return {
            "jobId": self.jobId,
            "kind": self.kind,
            "owner": self.owner,
            "method": self.method,
            "path": self.path,
            "status": self.status,
            "planId": self.planId,
            "createdAt": self.createdAt,
            "updatedAt": self.updatedAt,
            "params": self.params,
            "body": self.body,
            "headers": self.headers,
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, doc: dict[str, Any]) -> "JobRecord":
        """从存储文档还原；字段缺失 / 类型异常按缺省值处理（容忍旧版本记录）。"""
        return cls(
            jobId=str(doc.get("jobId") or ""),
            kind=str(doc.get("kind") or ""),
            owner=str(doc.get("owner") or ""),
            method=str(doc.get("method") or "POST"),
            path=str(doc.get("path") or ""),
            status=str(doc.get("status") or JOB_STATUS_PENDING),
            planId=doc.get("planId") if isinstance(doc.get("planId"), str) else None,
            createdAt=float(doc.get("createdAt") or time.time()),
            updatedAt=float(doc.get("updatedAt") or time.time()),
            params=doc.get("params") if isinstance(doc.get("params"), list) else [],
            body=doc.get("body") if isinstance(doc.get("body"), str) else None,
            headers=doc.get("headers") if isinstance(doc.get("headers"), dict) else {},
            progress=doc.get("progress") if isinstance(doc.get("progress"), list) else [],
            result=doc.get("result") if isinstance(doc.get("result"), dict) else None,
            error=doc.get("error") if isinstance(doc.get("error"), dict) else None,
        )

    def snapshot(self) -> dict[str, Any]:
        """对外快照：供 ``GET /api/jobs/{id}`` 与 SSE 终态事件使用。

        刻意**不含** ``body``（请求体可能包含内部参数）与 ``owner``；``result.data``
        仅在未超限时保留，客户端可据 ``planId`` 自行拉取完整详情。
        """
        return {
            "jobId": self.jobId,
            "kind": self.kind,
            "status": self.status,
            "planId": self.planId,
            "createdAt": self.createdAt,
            "updatedAt": self.updatedAt,
            "progress": list(self.progress),
            "result": self.result,
            "error": self.error,
        }


class JobStore:
    """作业存储：基于 :mod:`app.kvstore` 的键值后端（内存 / Redis）。"""

    def __init__(self, kv, *, ttl_seconds: float = 3600.0) -> None:
        self._kv = kv
        self._ttl = ttl_seconds

    @property
    def backend(self) -> str:
        return self._kv.name

    async def create(self, record: JobRecord) -> None:
        await self._kv.set(record.jobId, record.to_dict(), self._ttl)

    async def save(self, record: JobRecord) -> None:
        record.updatedAt = time.time()
        await self._kv.set(record.jobId, record.to_dict(), self._ttl)

    async def get(self, job_id: str) -> JobRecord | None:
        doc = await self._kv.get(job_id)
        if not isinstance(doc, dict):
            return None
        return JobRecord.from_dict(doc)

    async def close(self) -> None:
        await self._kv.close()

    def describe(self) -> dict[str, Any]:
        return {"ttlSeconds": self._ttl, **self._kv.describe()}


def build_job_store(
    *,
    driver: str = "auto",
    redis_url: str = "",
    max_entries: int = 1024,
    ttl_seconds: float = 3600.0,
) -> JobStore:
    """按配置构建作业存储（``driver=redis`` 但缺依赖 / URL 时启动即失败）。"""
    kv = build_kv_store(
        store="job",
        driver=driver,
        redis_url=redis_url,
        max_entries=max_entries,
        prefix=JOB_PREFIX,
    )
    return JobStore(kv, ttl_seconds=ttl_seconds)


# --------------------------------- 提交与执行 ---------------------------------


async def submit_job(
    ctx,
    *,
    kind: str,
    owner: str,
    method: str,
    path: str,
    body: bytes,
    params: list[list[str]],
    headers: dict[str, str],
) -> JobRecord:
    """受理一个作业：落库（pending）→ 后台起任务 → 返回记录。

    :raises ApiError: 未配置作业存储（503）/ 请求体超过作业载荷上限（413）。
    """
    if ctx.jobs is None:
        raise job_store_unavailable()
    if len(body) > JOB_MAX_REQUEST_BYTES:
        # 作业记录会整体写进状态存储：不设上限的话，「大 body × 多作业」可直接打爆存储
        raise payload_too_large(
            f"作业请求体超过上限（{JOB_MAX_REQUEST_BYTES} 字节），请拆分或改用同步接口"
        )

    record = JobRecord(
        jobId=uuid.uuid4().hex,
        kind=kind,
        owner=owner,
        method=method,
        path=path,
        params=params,
        body=base64.b64encode(body).decode("ascii") if body else None,
        headers=dict(headers),
    )
    _add_progress(record, JOB_PHASE_QUEUED, "作业已受理，正在排队执行")
    await ctx.jobs.create(record)
    _schedule(ctx, record.jobId)
    _audit("job.submit", record)
    METRICS.inc_counter("jobs_total", {"kind": kind, "outcome": "submitted"}, help="异步作业总数（按种类与结果分标签）")
    return record


def _schedule(ctx, job_id: str) -> None:
    """把作业执行放到后台任务；句柄登记在上下文里，便于取消与优雅关机。"""
    task = asyncio.create_task(_run_guarded(ctx, job_id), name=f"job:{job_id}")
    ctx.job_tasks[job_id] = task

    def _on_done(finished: asyncio.Task) -> None:
        ctx.job_tasks.pop(job_id, None)
        if finished.cancelled():
            return
        # 主动取回异常：否则 asyncio 会在 GC 时打印「Task exception was never retrieved」
        exc = finished.exception()
        if exc is not None:  # pragma: no cover - 兜底分支（正常路径已在内部收敛）
            logger.error("作业后台任务异常退出：%s（%s）", job_id, exc)

    task.add_done_callback(_on_done)


async def _run_guarded(ctx, job_id: str) -> None:
    """作业执行外壳：无论发生什么都必须把作业收敛为终态（否则客户端会一直轮询）。

    注意 ``asyncio.CancelledError`` 继承自 ``BaseException``，不会被 ``except Exception``
    捕获，必须单独处理；否则取消后作业会永远停在 ``running``。
    """
    try:
        await run_job(ctx, job_id)
    except asyncio.CancelledError:
        await _mark_cancelled(ctx, job_id)
        raise
    except Exception:  # noqa: BLE001 - 作业不能把异常留给调用方
        logger.exception("作业执行出现未捕获异常：%s", job_id)
        await _settle(
            ctx,
            job_id,
            status=JOB_STATUS_FAILED,
            error={"code": "INTERNAL_ERROR", "message": "作业执行失败，请稍后重试"},
        )


async def run_job(ctx, job_id: str) -> None:
    """执行作业：调用引擎并把结果收敛为作业终态。"""
    store = ctx.jobs
    if store is None:
        return
    record = await store.get(job_id)
    if record is None or record.terminal:
        return

    record.status = JOB_STATUS_RUNNING
    _add_progress(record, JOB_PHASE_ENGINE, "正在调用算法引擎（DAG 拓扑 / 关键路径 / 风险）")
    await store.save(record)
    _audit("job.start", record)

    started = time.monotonic()
    try:
        upstream = await ctx.engine.forward(
            record.method,
            record.path,
            params=[tuple(pair) for pair in record.params] or None,
            content=_decode_body(record),
            headers=dict(record.headers),
            timeout=ctx.settings.engine_job_timeout_seconds,
        )
    except ApiError as exc:
        # 只落稳定错误码与文案（本层错误文案不含上游地址 / 栈）
        await _settle(
            ctx,
            job_id,
            status=JOB_STATUS_FAILED,
            error={"code": exc.code, "message": exc.message},
            duration=time.monotonic() - started,
        )
        return
    except Exception:  # noqa: BLE001 - 兜底：任何异常都要收敛为终态
        logger.exception("作业调用引擎异常：%s %s", record.method, record.path)
        await _settle(
            ctx,
            job_id,
            status=JOB_STATUS_FAILED,
            error={"code": "ENGINE_ERROR", "message": "算法引擎调用失败，请稍后重试"},
            duration=time.monotonic() - started,
        )
        return

    body = upstream.content
    envelope = _parse_json(body)
    if upstream.status_code >= 400:
        code, message = _engine_error(envelope, upstream.status_code)
        await _settle(
            ctx,
            job_id,
            status=JOB_STATUS_FAILED,
            error={"code": code, "message": message},
            duration=time.monotonic() - started,
        )
        return

    plan_id = _extract_plan_id(envelope)
    result: dict[str, Any] = {
        "statusCode": upstream.status_code,
        "planId": plan_id,
        # 结果体只在未超限时保留：作业记录是状态、不是数据仓库
        "data": envelope if envelope is not None and _within_result_limit(envelope) else None,
    }
    await _settle(
        ctx,
        job_id,
        status=JOB_STATUS_SUCCEEDED,
        planId=plan_id,
        result=result,
        duration=time.monotonic() - started,
    )


async def cancel_job(ctx, job_id: str, *, owner: str | None) -> JobRecord:
    """取消作业：标记终态 + 尽力取消本地任务。

    :raises ApiError: 作业不存在（404，含 owner 不匹配，避免探测存在性）/ 已进入终态（409）。
    """
    store = ctx.jobs
    if store is None:
        raise job_store_unavailable()
    record = await store.get(job_id)
    if record is None or (owner is not None and record.owner != owner):
        # owner 不匹配与不存在同样回报 404：否则可被用来探测「别人有没有这个作业」
        raise job_not_found()
    if record.terminal:
        raise job_conflict("作业已结束，无法取消")

    # 先落终态再取消本地任务：这样即使取消请求落在「没有该任务句柄」的副本上（跨副本），
    # 执行方也会在收敛时看到 cancelled 并让位，而不是把作业改回成功。
    record.status = JOB_STATUS_CANCELLED
    _add_progress(record, JOB_PHASE_SETTLED, "作业已取消")
    await store.save(record)

    task = ctx.job_tasks.get(job_id)
    if task is not None and not task.done():
        task.cancel()
    _audit("job.cancel", record)
    return record


# --------------------------------- 内部辅助 ---------------------------------


async def _mark_cancelled(ctx, job_id: str) -> None:
    await _settle(ctx, job_id, status=JOB_STATUS_CANCELLED)


async def _settle(
    ctx,
    job_id: str,
    *,
    status: str,
    planId: str | None = None,
    result: dict[str, Any] | None = None,
    error: dict[str, str] | None = None,
    duration: float | None = None,
) -> None:
    """把作业收敛为终态（读改写；**取消优先**，绝不把已取消的作业改回成功）。"""
    store = ctx.jobs
    if store is None:
        return
    record = await store.get(job_id)
    if record is None:
        return

    if record.status == JOB_STATUS_CANCELLED and status != JOB_STATUS_CANCELLED:
        # 用户已取消，但引擎已完成计算（取消无法中断引擎）：
        # 如实记录结果与说明，而不是假装「什么都没发生」，也不谎报成功。
        if result is not None:
            record.result = result
        if planId:
            record.planId = planId
        _add_progress(record, JOB_PHASE_SETTLED, "作业已取消；算法引擎已完成本轮计算，结果已记录")
        await store.save(record)
        METRICS.inc_counter("jobs_total", {"kind": record.kind, "outcome": "cancelled"}, help="异步作业总数（按种类与结果分标签）")
        return

    record.status = status
    if planId:
        record.planId = planId
    if result is not None:
        record.result = result
    if error is not None:
        record.error = error
    _add_progress(record, JOB_PHASE_SETTLED, _settled_message(status, error))
    await store.save(record)
    _audit(f"job.{status}", record, durationMs=int((duration or 0) * 1000))
    METRICS.inc_counter("jobs_total", {"kind": record.kind, "outcome": status}, help="异步作业总数（按种类与结果分标签）")
    if duration is not None:
        METRICS.observe(
            "job_duration_seconds",
            duration,
            {"kind": record.kind},
            buckets=JOB_DURATION_BUCKETS,
            help="异步作业耗时（秒，按种类分标签）",
        )


def _settled_message(status: str, error: dict[str, str] | None) -> str:
    if status == JOB_STATUS_SUCCEEDED:
        return "算法引擎计算完成"
    if status == JOB_STATUS_CANCELLED:
        return "作业已取消"
    detail = (error or {}).get("code") or "UNKNOWN"
    return f"作业失败（{detail}）"


def _add_progress(record: JobRecord, phase: str, message: str) -> None:
    record.progress.append({"at": time.time(), "phase": phase, "message": message})
    if len(record.progress) > JOB_MAX_PROGRESS_ITEMS:
        del record.progress[: len(record.progress) - JOB_MAX_PROGRESS_ITEMS]


def _decode_body(record: JobRecord) -> bytes | None:
    if not record.body:
        return None
    try:
        return base64.b64decode(record.body)
    except (ValueError, TypeError):
        logger.warning("作业请求体解码失败：%s", record.jobId)
        return None


def _parse_json(body: bytes | None) -> Any | None:
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError):
        return None


def _within_result_limit(envelope: Any) -> bool:
    try:
        return len(json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))) <= JOB_MAX_RESULT_BYTES
    except (TypeError, ValueError):
        return False


def _engine_error(envelope: Any, status_code: int) -> tuple[str, str]:
    """从引擎错误信封提取稳定码与文案（契约见 ``src/api``：``{code, message}``）。"""
    if isinstance(envelope, dict) and isinstance(envelope.get("code"), str):
        message = envelope.get("message")
        return envelope["code"], message if isinstance(message, str) else "算法引擎返回错误"
    return "ENGINE_ERROR", f"算法引擎返回异常状态：{status_code}"


def _extract_plan_id(envelope: Any) -> str | None:
    """在引擎响应信封里递归找第一个 ``planId``（不依赖具体业务字段结构）。

    引擎的建计划 / 重规划响应结构可能随版本调整；作业层只需要一个可靠的「这次写落在
    哪个计划上」的锚点，因此按深度优先搜索第一个字符串型 ``planId``，避免与业务结构耦合。
    """

    stack: list[Any] = [envelope]
    seen = 0
    while stack and seen < 2_000:
        seen += 1
        node = stack.pop()
        if isinstance(node, dict):
            value = node.get("planId")
            if isinstance(value, str) and value:
                return value
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


def _audit(event: str, record: JobRecord, **fields: Any) -> None:
    """写链路审计日志（结构化字段，便于按作业 / 计划 / 用户检索）。"""
    from .logging_config import request_id_var

    logger.info(
        "作业审计：%s",
        event,
        extra={
            "extra_fields": {
                "audit": event,
                "jobId": record.jobId,
                "kind": record.kind,
                "planId": record.planId,
                "user": record.owner,
                "requestId": request_id_var.get() or "",
                **fields,
            }
        },
    )


__all__ = [
    "JOB_PREFIX",
    "JOB_DURATION_BUCKETS",
    "JobRecord",
    "JobStore",
    "build_job_store",
    "submit_job",
    "run_job",
    "cancel_job",
    "KvStoreError",
]

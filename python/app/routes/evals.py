"""Agent Evals 评测台端点（``{base}/evals/*``）。

评测台回答的是一个交付方最该回答、也最容易被跳过的问题：**改了 Prompt / 工具 / 编排逻辑
之后，Agent 到底变好了还是变坏了？**。没有它，任何「Agent 优化」都只能靠手点几下凭感觉判断。

两条对外能力：

- ``GET  {base}/evals``  评测台自述（黄金集清单 / 门禁阈值 / judge 开关 / 当前模型）
- ``POST {base}/evals``  触发一次评测运行

``POST`` 支持两种执行方式（与建计划的长耗时写同一范式）：

- **同步**（缺省）：直接返回完整 ``EvalSuiteResult``，适合前端点击「跑一遍」；
- **异步**（``enqueue=true``）：受理为**异步作业**（``kind=evals``）并回 ``jobId``，客户端用既有的
  ``GET {base}/jobs/{id}`` 与 SSE 进度流轮询 / 订阅。评测要真实驱动 Agent 跑多轮 LLM +
  引擎调用，在链路上很容易超过中间层超时——把它变成作业而不是挂住连接，才不会出现
  「客户端看到超时、评测其实已跑完」的假失败。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from contextlib import suppress
from typing import Any

from fastapi import APIRouter, Request

from ..constants import (
    JOB_MAX_PROGRESS_ITEMS,
    JOB_MAX_RESULT_BYTES,
    JOB_PHASE_QUEUED,
    JOB_PHASE_SETTLED,
    JOB_STATUS_FAILED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
)
from ..errors import ApiError, bad_request, job_store_unavailable, not_found
from ..evals.golden import cases_for
from ..evals.incidents import derive_case
from ..jobs import JobRecord
from ..kvstore import KvStoreError
from ..metrics import METRICS
from ..models import EvalRunRequest, IncidentCaseRequest
from ..state import AppContext

logger = logging.getLogger("iteration_planner.evals.api")

#: 自定义用例 ID 字符集（会拼进存储键，必须限制，否则路径参数可被用来构造任意键）
_CASE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
#: 执行轨迹 ID 恒为 ``uuid4().hex``（32 位十六进制）
_TRACE_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")

#: 评测台不可用（``EVALS_ENABLED=false``）
_DISABLED = "评测台未启用（EVALS_ENABLED=false）"
#: 自定义（回归）用例存储不可用（``EVALS_CUSTOM_CASES_ENABLED=false``）
_CASES_DISABLED = "自定义评测用例未启用（EVALS_CUSTOM_CASES_ENABLED=false）"


def _require_evals(ctx: AppContext):
    if ctx.evals is None:
        raise ApiError(503, "EVALS_DISABLED", _DISABLED)
    return ctx.evals


def _require_cases(ctx: AppContext):
    if ctx.custom_cases is None:
        raise ApiError(503, "EVALS_CASES_DISABLED", _CASES_DISABLED)
    return ctx.custom_cases


def _kv_unavailable(exc: Exception) -> ApiError:
    """自定义用例是**状态数据**：存储不可用时显式失败（映射 503），不静默当作「没有用例」——
    静默会让「事故已经沉淀过用例」这件事看起来从未发生，从而重复沉淀。"""
    logger.warning("自定义用例存储不可用：%s", exc)
    return ApiError(503, "EVALS_CASE_STORE_UNAVAILABLE", "评测用例存储暂不可用，请稍后重试")


def _owner_of(request: Request) -> str:
    principal = getattr(request.state, "principal", None)
    return principal.user_id if principal is not None else ""


def _progress(record: JobRecord, phase: str, message: str) -> None:
    """追加作业进度（与 :mod:`app.jobs` 同一上限裁剪规则）。"""
    record.progress.append({"at": time.time(), "phase": phase, "message": message})
    if len(record.progress) > JOB_MAX_PROGRESS_ITEMS:
        del record.progress[: len(record.progress) - JOB_MAX_PROGRESS_ITEMS]


def _suite_payload(suite: Any) -> dict[str, Any]:
    """把评测结果放进作业结果体；超限时**只丢明细、保留汇总与门禁结论**。"""
    payload = suite.model_dump()
    try:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return {"runId": suite.runId, "note": "评测结果无法序列化（已丢弃明细）"}
    if len(encoded) <= JOB_MAX_RESULT_BYTES:
        return payload
    payload["cases"] = []
    payload.setdefault("notes", []).append("评测明细超出作业结果上限，已仅保留汇总：请缩小 caseIds 后再跑。")
    return payload


async def _execute_eval_job(ctx: AppContext, job_id: str, payload: EvalRunRequest) -> None:
    """异步评测作业：把评测结果收敛为作业终态（无论成功失败都必须落地终态）。"""
    store = ctx.jobs
    if store is None:
        return
    record = await store.get(job_id)
    if record is None or record.terminal:
        return
    # 阶段序列固定为 queued → settled（评测不经过算法引擎，因此没有 engine 阶段）；
    # 受理时已记过 queued，这里只翻状态，不重复追加同一阶段
    record.status = JOB_STATUS_RUNNING
    await store.save(record)

    try:
        suite = await ctx.evals.run(payload)
    except ApiError as exc:
        record.status = JOB_STATUS_FAILED
        record.error = {"code": exc.code, "message": exc.message}
        _progress(record, JOB_PHASE_SETTLED, f"评测失败（{exc.code}）")
        await store.save(record)
        return
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - 评测异常也必须收敛终态，否则客户端一直轮询
        logger.exception("评测作业异常：%s", job_id)
        record.status = JOB_STATUS_FAILED
        record.error = {"code": "INTERNAL_ERROR", "message": "评测执行失败，请稍后重试"}
        _progress(record, JOB_PHASE_SETTLED, "评测失败（INTERNAL_ERROR）")
        await store.save(record)
        return

    record.status = JOB_STATUS_SUCCEEDED
    record.result = {"statusCode": 200, "planId": None, "data": _suite_payload(suite)}
    _progress(
        record,
        JOB_PHASE_SETTLED,
        f"评测完成：通过 {suite.passed} / 失败 {suite.failed} / 异常 {suite.errored}"
        + ("，门禁通过" if suite.gate and suite.gate.passed else "，门禁未通过"),
    )
    await store.save(record)
    METRICS.inc_counter("eval_runs_total", {"gate": "passed" if suite.gate and suite.gate.passed else "failed"})


async def _enqueue(ctx: AppContext, request: Request, payload: EvalRunRequest) -> dict[str, Any]:
    """把评测受理为异步作业（复用既有作业端点做轮询 / 进度流）。"""
    store = ctx.jobs
    if store is None:
        raise job_store_unavailable()
    record = JobRecord(
        jobId=uuid.uuid4().hex,
        kind="evals",
        owner=_owner_of(request),
        method="POST",
        path="evals",
    )
    _progress(record, JOB_PHASE_QUEUED, "评测作业已受理，正在排队执行")
    await store.create(record)

    task = asyncio.create_task(_execute_eval_job(ctx, record.jobId, payload), name=f"evals:{record.jobId}")
    ctx.job_tasks[record.jobId] = task

    def _on_done(finished: asyncio.Task) -> None:
        ctx.job_tasks.pop(record.jobId, None)
        if finished.cancelled():
            return
        exc = finished.exception()
        if exc is not None:  # pragma: no cover - 正常路径已在内部收敛
            logger.error("评测作业后台任务异常退出：%s（%s）", record.jobId, exc)

    task.add_done_callback(_on_done)
    return record.snapshot()


def build_evals_router(base: str) -> APIRouter:
    router = APIRouter(prefix=f"{base}/evals", tags=["evals"])

    @router.get("", summary="评测台自述（黄金集清单 / 门禁阈值 / judge 开关 / 当前模型）")
    async def evals_describe(request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        service = _require_evals(ctx)
        data = await service.describe()
        data["enabled"] = True
        data["jobBackend"] = ctx.jobs.backend if ctx.jobs is not None else ""
        data["traceBackend"] = ctx.traces.backend if ctx.traces is not None else ""
        data["customCaseBackend"] = ctx.custom_cases.backend if ctx.custom_cases is not None else ""
        return {"data": data}

    @router.post("", summary="触发一次评测运行（同步返回结果；enqueue=true 则转为异步作业）")
    async def evals_run(payload: EvalRunRequest, request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        service = _require_evals(ctx)
        if not ctx.runner.available and not payload.kinds:
            # 无 LLM 时只有 ``reflection`` 类（确定性演练）能跑：提前给出明确指引，
            # 而不是让用户等到一整批用例全变 error 才发现原因
            raise bad_request("未配置 LLM：仅 reflection 类确定性演练可执行，请用 kinds=[\"reflection\"]")
        if payload.enqueue:
            return {"data": await _enqueue(ctx, request, payload)}
        suite = await service.run(payload)
        return {"data": suite.model_dump()}

    # ------------------ 自定义（回归）用例：事故 → 用例 → 门禁 闭环 ------------------

    @router.get("/cases", summary="自定义评测用例列表（由失败轨迹沉淀 / 人工登记，自动纳入门禁）")
    async def cases_list(request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        store = _require_cases(ctx)
        try:
            items = await store.list()
        except KvStoreError as exc:
            raise _kv_unavailable(exc) from exc
        return {
            "data": {
                "items": [item.model_dump() for item in items],
                "total": len(items),
                "enabled": True,
                "backend": store.backend,
                **store.describe(),
            }
        }

    @router.get("/cases/effective", summary="当前生效的用例集（黄金集 + 自定义），供核对门禁覆盖面")
    async def cases_effective(request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        custom = await ctx.evals.custom_cases() if ctx.evals is not None else ()
        merged = cases_for(extra=custom)
        golden_ids = {case.id for case in cases_for()}
        return {
            "data": {
                "total": len(merged),
                "cases": [case.model_dump() for case in merged],
                "customIds": [case.id for case in merged if case.id not in golden_ids],
            }
        }

    @router.post("/incidents", summary="把一条失败轨迹沉淀为回归用例（确定性推导断言，幂等覆盖）")
    async def incident_create(payload: IncidentCaseRequest, request: Request) -> dict:
        """事故 → 用例闭环的入口。

        断言由 :func:`app.evals.incidents.derive_case` **确定性推导**（同一个工具失败 →
        要求不再失败；护栏被触发 → 要求归零），不依赖人写断言，也不依赖模型判断——
        沉淀下来的用例必须可复核、可解释，否则它只是又一段「看起来像测试」的文字。
        """
        ctx: AppContext = request.app.state.ctx
        store = _require_cases(ctx)
        if ctx.traces is None:
            raise ApiError(503, "TRACE_STORE_UNAVAILABLE", "执行轨迹未启用，无法由轨迹沉淀用例")
        if not _TRACE_ID_RE.match(payload.traceId):
            raise not_found("执行轨迹不存在")
        try:
            trace = await ctx.traces.get(payload.traceId.lower())
        except KvStoreError as exc:
            raise ApiError(503, "TRACE_STORE_UNAVAILABLE", "执行轨迹存储暂不可用，请稍后重试") from exc
        if trace is None or trace.owner != _owner_of(request):
            raise not_found("执行轨迹不存在")

        item = derive_case(
            trace, title=payload.title, note=payload.note or "由失败轨迹自动沉淀", cleanup=payload.cleanup
        )
        try:
            saved = await store.save(
                case=item.case,
                origin=item.origin,
                origin_trace_id=item.originTraceId,
                note=item.note,
                owner=item.owner or _owner_of(request),
                rationale=item.rationale,
            )
        except KvStoreError as exc:
            raise _kv_unavailable(exc) from exc
        # 回填「该轨迹已沉淀」：前端据此隐藏重复入口，避免同一事故被反复沉淀成多条用例
        with suppress(Exception):
            await ctx.traces.attach_incident_case(trace.traceId, saved.case.id)
        METRICS.inc_counter("evals_incident_case_total")
        custom = await ctx.evals.custom_cases() if ctx.evals is not None else await store.specs()
        return {
            "data": {
                **saved.model_dump(),
                "traceId": trace.traceId,
                # 生效集 = 黄金集 + 自定义用例：沉淀必须**立刻**体现在门禁覆盖面上（否则等于没沉淀）
                "effectiveCaseCount": len(cases_for(extra=custom)),
            }
        }

    @router.delete("/cases/{case_id}", summary="删除一条自定义用例（不再纳入门禁）")
    async def case_delete(case_id: str, request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        store = _require_cases(ctx)
        if not _CASE_ID_RE.match(case_id):
            raise not_found("用例不存在")
        try:
            existing = await store.get(case_id)
            if existing is None or (existing.owner and existing.owner != _owner_of(request)):
                raise not_found("用例不存在")
            await store.delete(case_id)
        except KvStoreError as exc:
            raise _kv_unavailable(exc) from exc
        return {"data": {"caseId": case_id, "deleted": True}}

    return router


__all__ = ["build_evals_router"]

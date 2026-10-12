"""What-if 情景对比端点（``/api/scenario/*``）。

在**需求原文不变**的前提下，用不同的迭代约束（截止日期 / 起始日期 / 开发人数）
重新跑一次规划，并与基线计划做指标对比，回答「加人 / 延期能不能按期交付」。

实现严格复用引擎能力，不复制任何算法：
1. 读取基线计划（``GET /plans/{id}``）取得 ``rawRequirement`` 与约束；
2. 以相同需求 + 情景约束新建一个**临时计划**（``POST /plans``，走完整算法流水线）；
3. 汇总两者指标（工期 / 工时 / 任务数 / 关键任务数 / 风险数）并计算差值；
4. 无论成功与否，``finally`` 中回收临时计划（``DELETE /plans/{id}``），避免污染存储。

说明：规则拆解器（``RuleBasedDecomposer``）对同一需求是确定性的，``devMemberCount``
仅影响任务指派，因此「同需求不同约束」的对比口径有效；跨需求对比不在本端点范围内。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request

from ..engine_client import EngineClient
from ..errors import ApiError, bad_request
from ..models import (
    ScenarioCompareRequest,
    ScenarioCompareResponse,
    ScenarioConstraints,
    ScenarioDelta,
    ScenarioMetrics,
)
from ..state import AppContext

logger = logging.getLogger("iteration_planner.scenario")


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _metrics(plan: dict[str, Any]) -> ScenarioMetrics:
    """从引擎返回的计划快照中汇总对比指标（不引入任何算法推导）。"""
    tasks = plan.get("taskList") or []
    risks = [risk for risk in (plan.get("riskList") or []) if isinstance(risk, dict)]
    burndown = plan.get("burndownRawData") or []
    # 燃尽数据的末点即引擎推算的计划完成日期
    planned_end = burndown[-1].get("time") if burndown else None
    deadline = plan.get("iterationDeadline")
    deadline_met: bool | None = None
    if planned_end and deadline:
        # ISO 日期（YYYY-MM-DD）可直接按字典序比较先后
        deadline_met = str(planned_end) <= str(deadline)
    return ScenarioMetrics(
        totalDurationHours=_as_float(plan.get("totalDurationHours")),
        totalEstimatedHours=_as_float(plan.get("totalEstimatedHours")),
        taskCount=len(tasks),
        criticalTaskCount=len(plan.get("criticalPath") or []),
        riskCount=len(risks),
        highRiskCount=sum(1 for risk in risks if str(risk.get("level")).upper() == "HIGH"),
        plannedEndDate=str(planned_end) if planned_end else None,
        deadline=str(deadline) if deadline else None,
        deadlineMet=deadline_met,
    )


def _delta(baseline: ScenarioMetrics, scenario: ScenarioMetrics) -> ScenarioDelta:
    """情景 − 基线。"""
    return ScenarioDelta(
        totalDurationHours=round(scenario.totalDurationHours - baseline.totalDurationHours, 4),
        totalEstimatedHours=round(scenario.totalEstimatedHours - baseline.totalEstimatedHours, 4),
        taskCount=scenario.taskCount - baseline.taskCount,
        criticalTaskCount=scenario.criticalTaskCount - baseline.criticalTaskCount,
        riskCount=scenario.riskCount - baseline.riskCount,
        highRiskCount=scenario.highRiskCount - baseline.highRiskCount,
    )


async def _discard(engine: EngineClient, plan_id: str | None) -> None:
    """尽力回收临时计划：失败仅告警，不影响已完成的对比结果。"""
    if not plan_id:
        return
    try:
        await engine.request("DELETE", f"/plans/{plan_id}")
    except ApiError as exc:
        logger.warning("临时情景计划回收失败（planId=%s）：%s", plan_id, exc.message)
    except Exception:  # noqa: BLE001 - 回收失败不应影响主流程
        logger.exception("临时情景计划回收出现未预期异常（planId=%s）", plan_id)


def build_scenario_router(base: str) -> APIRouter:
    router = APIRouter(prefix=f"{base}/scenario", tags=["scenario"])

    @router.post("/compare", summary="What-if 情景对比：同需求变更约束后重新规划并对比指标")
    async def compare(payload: ScenarioCompareRequest, request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        baseline = await ctx.engine.request("GET", f"/plans/{payload.planId}")

        requirement = str(baseline.get("rawRequirement") or "").strip()
        if not requirement:
            raise bad_request("基线计划缺少原始需求文本，无法进行情景对比")

        baseline_constraints = ScenarioConstraints(
            iterationDeadline=baseline.get("iterationDeadline"),
            iterationStartDate=baseline.get("iterationStartDate"),
            devMemberCount=baseline.get("devMemberCount"),
        )
        scenario_constraints = ScenarioConstraints(
            iterationDeadline=payload.iterationDeadline or baseline_constraints.iterationDeadline,
            iterationStartDate=payload.iterationStartDate or baseline_constraints.iterationStartDate,
            devMemberCount=payload.devMemberCount or baseline_constraints.devMemberCount,
        )
        baseline_metrics = _metrics(baseline)

        temp_plan: dict[str, Any] | None = None
        try:
            temp_plan = await ctx.engine.request(
                "POST",
                "/plans",
                json={
                    "requirement": requirement,
                    "iterationDeadline": scenario_constraints.iterationDeadline,
                    "iterationStartDate": scenario_constraints.iterationStartDate,
                    "devMemberCount": scenario_constraints.devMemberCount,
                },
            )
            scenario_metrics = _metrics(temp_plan)
        finally:
            await _discard(ctx.engine, temp_plan.get("planId") if isinstance(temp_plan, dict) else None)

        result = ScenarioCompareResponse(
            planId=payload.planId,
            baselineConstraints=baseline_constraints,
            scenarioConstraints=scenario_constraints,
            baseline=baseline_metrics,
            scenario=scenario_metrics,
            delta=_delta(baseline_metrics, scenario_metrics),
        )
        return {"data": result.model_dump()}

    return router


__all__ = ["build_scenario_router"]
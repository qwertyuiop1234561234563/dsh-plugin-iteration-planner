"""What-if 沙盘：自然语言情景 → 引擎 AOE 重算 → before/after 增量对比。

与既有 ``POST /api/scenario/compare`` 的关系：后者只支持**约束级**改动（截止日期 / 起始日期 /
开发人数）且需要调用方给出结构化参数；本模块在其基础上增加：

- **自然语言入口**：一句话（「后端延期 3 天」「再加 2 个人」「把接口开发的工时提到 12 小时」）
  先经 LLM（或规则降级）解析为结构化情景参数；
- **任务级扰动**：允许对单个任务改工时 / 责任人 / 优先级 / 状态，通过引擎 ``PATCH`` 落地；
- **增量对比**：除指标差值外，额外给出关键路径增删、新增 / 解除风险、交付日期偏移与逐条改动的
  before/after。

算法边界：情景重算与任务扰动**全部由引擎完成**（``POST /plans`` 走完整流水线、``PATCH``
触发全量重规划），本模块只做参数解析与结果汇总，不重算任何工期 / 关键路径 / 风险。

一致性：情景计划是**临时计划**，无论成功失败都在 ``finally`` 中回收，不污染持久化存储
（与 ``/api/scenario/compare`` 同一约定）。
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any

from ..engine_client import EngineClient
from ..errors import ApiError, bad_request
from ..models import ScenarioConstraints, ScenarioDelta, ScenarioMetrics, WhatIfResponse, WhatIfTaskEdit
from .analytics import (
    critical_path,
    day_offset,
    metrics_delta,
    parse_date,
    plan_metrics,
    task_names,
)
from .json_utils import extract_json_object
from .prompts import WHATIF_PARSER_SYSTEM_PROMPT, build_whatif_instruction
from .runner import AgentRunner
from .sanitize import unwrap_untrusted
from .schemas import UPDATABLE_FIELDS

logger = logging.getLogger("iteration_planner.whatif")

#: 规则解析（LLM 不可用时的降级路径）支持的时间单位 → 天数
_DAY_UNITS = {"天": 1, "日": 1, "周": 7, "星期": 7, "工作日": 1}
_DELAY_RE = re.compile(r"(延期|延后|推迟|顺延)\s*(\d+(?:\.\d+)?)\s*(个)?\s*(天|日|周|星期|工作日)")
_ADVANCE_RE = re.compile(r"提前\s*(\d+(?:\.\d+)?)\s*(个)?\s*(天|日|周|星期|工作日)")
_ADD_MEMBER_RE = re.compile(r"(?:增加|加|再来|补充)\s*(\d+)\s*(?:个|名|位)?\s*(?:开发|研发|人)")
_CUT_MEMBER_RE = re.compile(r"(?:减少|减|调走|去掉)\s*(\d+)\s*(?:个|名|位)?\s*(?:开发|研发|人)")
_HOURS_RE = re.compile(r"把\s*(.+?)\s*的?\s*工时\s*(?:改|提|调|增)(?:高)?(?:成|到|为|至)?\s*(\d+(?:\.\d+)?)\s*(?:小时|h|H)")
_ASSIGNEE_RE = re.compile(r"把\s*(.+?)\s*的\s*(?:责任人|负责人)\s*(?:改|设|指派)(?:成|为|给)?\s*([^\s，,。]+)")
_STATUS_RE = re.compile(r"把\s*(.+?)\s*(?:状态)?\s*(?:改|设|标记)(?:成|为)\s*(TODO|IN_PROGRESS|BLOCKED|TESTING|DONE|DELAYED)")


def _rule_parse(instruction: str, baseline: dict[str, Any], tasks: list[dict[str, Any]]) -> dict[str, Any]:
    """确定性规则解析（无 LLM 时的降级路径；解析不出内容时返回 ``unparsed``）。"""
    constraints: dict[str, Any] = {}
    edits: list[dict[str, Any]] = []
    notes: list[str] = []
    deadline = parse_date(baseline.get("iterationDeadline"))
    start = parse_date(baseline.get("iterationStartDate"))
    members = baseline.get("devMemberCount")

    match = _DELAY_RE.search(instruction)
    if match and deadline:
        days = int(float(match.group(2)) * _DAY_UNITS.get(match.group(4), 1))
        constraints["iterationDeadline"] = (deadline + timedelta(days=days)).isoformat()
    else:
        match = _ADVANCE_RE.search(instruction)
        if match and deadline:
            days = int(float(match.group(1)) * _DAY_UNITS.get(match.group(3), 1))
            constraints["iterationDeadline"] = (deadline - timedelta(days=days)).isoformat()
    if start and "iterationStartDate" not in constraints:
        match = _DELAY_RE.search(instruction)
        if match and "iterationDeadline" not in constraints:
            days = int(float(match.group(2)) * _DAY_UNITS.get(match.group(4), 1))
            constraints["iterationStartDate"] = (start + timedelta(days=days)).isoformat()
    if isinstance(members, int):
        match = _ADD_MEMBER_RE.search(instruction)
        if match:
            constraints["devMemberCount"] = members + int(match.group(1))
        else:
            match = _CUT_MEMBER_RE.search(instruction)
            if match:
                constraints["devMemberCount"] = max(1, members - int(match.group(1)))

    match = _HOURS_RE.search(instruction)
    if match:
        edits.append({"task": match.group(1).strip(), "field": "estimatedHours", "value": float(match.group(2))})
    else:
        match = _ASSIGNEE_RE.search(instruction)
        if match:
            edits.append({"task": match.group(1).strip(), "field": "assignee", "value": match.group(2).strip()})
    match = _STATUS_RE.search(instruction)
    if match:
        edits.append({"task": match.group(1).strip(), "field": "status", "value": match.group(2)})

    if not constraints and not edits:
        notes.append("规则解析未能从该描述中提取出可执行的改动，请改用更明确的说法（如「截止日期延后 3 天」「把 X 的工时改成 12 小时」）")
    return {"intent": instruction, "constraints": constraints, "taskEdits": edits, "notes": notes, "unparsed": not (constraints or edits)}


def _normalize_constraints(raw: Any, baseline: dict[str, Any]) -> dict[str, Any]:
    """校验并归一化 LLM / 规则给出的约束（只保留合法且与基线不同的字段）。"""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("iterationDeadline", "iterationStartDate"):
        value = raw.get(key)
        if isinstance(value, str) and parse_date(value):
            parsed = parse_date(value)
            assert parsed is not None
            out[key] = parsed.isoformat()
    count = raw.get("devMemberCount")
    if isinstance(count, bool):
        count = None
    if isinstance(count, (int, float)) and int(count) > 0:
        out["devMemberCount"] = int(count)
    # 与基线完全一致的情景没有意义：直接剔除，避免「仿真出来的差异」全是 0 而误导用户
    for key in list(out):
        if str(out[key]) == str(baseline.get(key)):
            out.pop(key)
    return out


def _resolve_task(tasks: list[dict[str, Any]], ref: str) -> dict[str, Any] | None:
    """把任务引用（taskId / 任务名）解析到具体任务：精确 → 忽略大小写 → 包含匹配。"""
    text = (ref or "").strip()
    if not text:
        return None
    for task in tasks:
        if str(task.get("taskId")) == text:
            return task
    lowered = text.lower()
    for task in tasks:
        if str(task.get("taskName") or "").strip().lower() == lowered:
            return task
    for task in tasks:
        name = str(task.get("taskName") or "").strip().lower()
        if name and (lowered in name or name in lowered):
            return task
    return None


def _coerce_value(field: str, value: Any) -> Any:
    """按字段类型收敛取值（工时转 float，枚举转大写，依赖转数组）。"""
    if field == "estimatedHours":
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            raise bad_request("工时必须是数字") from None
        if parsed <= 0:
            raise bad_request("工时必须大于 0")
        return parsed
    if field in ("status", "priority"):
        return str(value).strip().upper()
    if field == "predecessorTaskIds":
        if isinstance(value, list):
            return [str(item) for item in value]
        return [item for item in re.split(r"[,\s，、]+", str(value)) if item]
    return str(value).strip()


async def _parse_instruction(
    runner: AgentRunner,
    *,
    instruction: str,
    baseline: dict[str, Any],
    model: str | None,
) -> tuple[dict[str, Any], str, str | None]:
    """解析用户描述：LLM 优先，未配置 / 失败时降级为规则解析。

    返回 ``(解析结果, mode, degradedReason)``。降级是**明确告知**的（``mode=rule`` +
    原因），不静默把规则结果冒充成模型理解。
    """
    tasks = [task for task in (baseline.get("taskList") or []) if isinstance(task, dict)]
    if runner.available:
        try:
            raw = await runner.complete(
                [
                    {"role": "system", "content": WHATIF_PARSER_SYSTEM_PROMPT},
                    {"role": "user", "content": build_whatif_instruction(instruction, baseline)},
                ],
                temperature=0.0,
            )
            parsed = extract_json_object(raw)
            if parsed is not None:
                return (
                    {
                        "intent": str(parsed.get("intent") or instruction),
                        "constraints": parsed.get("constraints") or {},
                        "taskEdits": parsed.get("taskEdits") or [],
                        "unparsed": bool(parsed.get("unparsed")),
                        "notes": [],
                    },
                    "llm",
                    None,
                )
            logger.warning("What-if 解析输出不是合法 JSON，回退规则解析")
        except ApiError as exc:
            logger.warning("What-if 解析调用模型失败（%s），回退规则解析", exc.message)
    else:
        logger.info("未配置 LLM，What-if 使用规则解析")
    fallback = _rule_parse(instruction, baseline, tasks)
    reason = "未配置 LLM" if not runner.available else "模型输出无法解析"
    return fallback, "rule", f"{reason}，已使用规则解析（支持「延后 N 天」「加/减 N 人」「把 X 的工时改成 N 小时」「把 X 的责任人改成 Y」）"


async def _discard(engine: EngineClient, plan_id: str | None) -> None:
    """回收临时情景计划（失败仅告警，不影响已产出的对比结果）。"""
    if not plan_id:
        return
    try:
        await engine.request("DELETE", f"/plans/{plan_id}")
    except ApiError as exc:
        logger.warning("临时情景计划回收失败（planId=%s）：%s", plan_id, exc.message)
    except Exception:  # noqa: BLE001 - 回收失败不应影响主流程
        logger.exception("临时情景计划回收出现未预期异常（planId=%s）", plan_id)


def _risk_descriptions(plan: dict[str, Any]) -> dict[str, str]:
    """风险指纹 → 描述（用描述文本作指纹，避免两次规划的风险 ID 必然不同）。"""
    out: dict[str, str] = {}
    for risk in plan.get("riskList") or []:
        if isinstance(risk, dict) and risk.get("description"):
            out[str(risk["description"])] = str(risk.get("description"))
    return out


async def run_whatif(
    engine: EngineClient,
    runner: AgentRunner,
    *,
    plan_id: str,
    instruction: str,
    model: str | None = None,
    max_task_edits: int = 10,
) -> WhatIfResponse:
    """执行一次自然语言 What-if 仿真并返回 before/after 增量对比。"""
    baseline_plan = await engine.request("GET", f"/plans/{plan_id}")
    requirement = unwrap_untrusted(str(baseline_plan.get("rawRequirement") or "")).strip()
    if not requirement:
        raise bad_request("基线计划缺少原始需求文本，无法进行情景仿真")

    parsed, mode, degraded = await _parse_instruction(
        runner, instruction=instruction, baseline=baseline_plan, model=model
    )
    notes: list[str] = list(parsed.get("notes") or [])
    constraints = _normalize_constraints(parsed.get("constraints"), baseline_plan)
    baseline_constraints = ScenarioConstraints(
        iterationDeadline=baseline_plan.get("iterationDeadline"),
        iterationStartDate=baseline_plan.get("iterationStartDate"),
        devMemberCount=baseline_plan.get("devMemberCount"),
    )
    scenario_constraints = ScenarioConstraints(
        iterationDeadline=constraints.get("iterationDeadline") or baseline_constraints.iterationDeadline,
        iterationStartDate=constraints.get("iterationStartDate") or baseline_constraints.iterationStartDate,
        devMemberCount=constraints.get("devMemberCount") or baseline_constraints.devMemberCount,
    )
    if not constraints and not (parsed.get("taskEdits") or []):
        notes.append("该描述未产生任何约束或任务扰动，情景与基线一致，因此对比结果为空")

    baseline_metrics = ScenarioMetrics(**plan_metrics(baseline_plan))
    edits: list[WhatIfTaskEdit] = []
    temp_id: str | None = None
    try:
        temp_plan = await engine.request(
            "POST",
            "/plans",
            json={
                "requirement": requirement,
                "iterationDeadline": scenario_constraints.iterationDeadline,
                "iterationStartDate": scenario_constraints.iterationStartDate,
                "devMemberCount": scenario_constraints.devMemberCount,
            },
        )
        temp_id = str(temp_plan.get("planId")) if isinstance(temp_plan, dict) else None
        # 任务级扰动：逐条经引擎 PATCH 落地（每次都触发全量重规划，与交互式改动同一条路径）
        raw_edits = [item for item in (parsed.get("taskEdits") or []) if isinstance(item, dict)]
        if len(raw_edits) > max_task_edits:
            notes.append(f"任务级扰动超过上限 {max_task_edits} 条，仅应用前 {max_task_edits} 条")
            raw_edits = raw_edits[:max_task_edits]
        for item in raw_edits:
            field = str(item.get("field") or "").strip()
            ref = str(item.get("task") or "").strip()
            entry = WhatIfTaskEdit(
                task=ref,
                field=field,
                value=item.get("value"),
                reason=str(item.get("reason") or "") or None,
                applied=False,
            )
            if field not in UPDATABLE_FIELDS:
                entry.error = "该字段不可修改（支持：预估工时 / 前置依赖 / 状态 / 优先级 / 责任人）"
                edits.append(entry)
                continue
            tasks = [task for task in (temp_plan.get("taskList") or []) if isinstance(task, dict)]
            target = _resolve_task(tasks, ref)
            if target is None:
                entry.error = "情景计划中找不到该任务（任务名需与计划中的任务名一致）"
                edits.append(entry)
                continue
            entry.taskId = str(target.get("taskId"))
            entry.task = str(target.get("taskName") or ref)
            entry.before = target.get(field)
            try:
                value = _coerce_value(field, item.get("value"))
                response = await engine.request(
                    "PATCH",
                    f"/plans/{temp_id}/tasks/{entry.taskId}",
                    json={"updateField": field, "newValue": value},
                )
            except ApiError as exc:
                entry.error = exc.message
                edits.append(entry)
                continue
            if isinstance(response, dict):
                updated = response.get("task") if isinstance(response.get("task"), dict) else {}
                entry.after = updated.get(field, value)
                if isinstance(response.get("plan"), dict):
                    temp_plan = response["plan"]
            entry.applied = True
            edits.append(entry)
        if temp_id:
            # 重新取一次快照，确保后续对比基于**全部扰动落地后**的计划（PATCH 已返回最新计划，
            # 但多副本 / 缓存场景下以读取为准，避免拿中间态做对比）
            refreshed = await engine.request("GET", f"/plans/{temp_id}")
            if isinstance(refreshed, dict):
                temp_plan = refreshed
        scenario_metrics = ScenarioMetrics(**plan_metrics(temp_plan))
    finally:
        await _discard(engine, temp_id)

    baseline_critical = critical_path(baseline_plan)
    scenario_critical = critical_path(temp_plan)
    baseline_risks = _risk_descriptions(baseline_plan)
    scenario_risks = _risk_descriptions(temp_plan)
    added = [name for name in task_names(temp_plan, scenario_critical) if name not in set(task_names(baseline_plan, baseline_critical))]
    removed = [name for name in task_names(baseline_plan, baseline_critical) if name not in set(task_names(temp_plan, scenario_critical))]

    summary = _summarize(
        intent=parsed.get("intent") or instruction,
        baseline=baseline_metrics,
        scenario=scenario_metrics,
        added=added,
        removed=removed,
        new_risks=list(scenario_risks.keys() - baseline_risks.keys()),
        resolved_risks=list(baseline_risks.keys() - scenario_risks.keys()),
        edits=edits,
    )
    return WhatIfResponse(
        planId=plan_id,
        mode=mode,
        model=model if mode == "llm" else None,
        degradedReason=degraded,
        intent=str(parsed.get("intent") or instruction),
        constraints=scenario_constraints,
        baselineConstraints=baseline_constraints,
        baseline=baseline_metrics,
        scenario=scenario_metrics,
        delta=ScenarioDelta(**metrics_delta(baseline_metrics.model_dump(), scenario_metrics.model_dump())),
        deadlineShiftDays=day_offset(scenario_metrics.plannedEndDate, baseline_metrics.plannedEndDate),
        criticalPathAdded=added,
        criticalPathRemoved=removed,
        newRisks=list(scenario_risks.keys() - baseline_risks.keys()),
        resolvedRisks=list(baseline_risks.keys() - scenario_risks.keys()),
        edits=edits,
        unparsed=bool(parsed.get("unparsed")) and not edits and not constraints,
        summary=summary,
        notes=notes,
        ephemeral=True,
    )


def _summarize(
    *,
    intent: str,
    baseline: ScenarioMetrics,
    scenario: ScenarioMetrics,
    added: list[str],
    removed: list[str],
    new_risks: list[str],
    resolved_risks: list[str],
    edits: list[WhatIfTaskEdit],
) -> str:
    """确定性生成一句话研判（不依赖 LLM，保证降级路径也有可读结论）。"""
    parts: list[str] = [f"情景「{intent}」"]
    duration_delta = round(scenario.totalDurationHours - baseline.totalDurationHours, 1)
    if duration_delta:
        parts.append(f"总工期{'增加' if duration_delta > 0 else '减少'} {abs(duration_delta)} 小时")
    else:
        parts.append("总工期不变")
    shift = day_offset(scenario.plannedEndDate, baseline.plannedEndDate)
    if shift:
        parts.append(f"预计完成日期{'推迟' if shift > 0 else '提前'} {abs(shift)} 天（{scenario.plannedEndDate}）")
    elif scenario.plannedEndDate:
        parts.append(f"预计完成日期不变（{scenario.plannedEndDate}）")
    if scenario.deadlineMet is False and baseline.deadlineMet is not False:
        parts.append("交付日期不再满足截止要求")
    elif scenario.deadlineMet is True and baseline.deadlineMet is False:
        parts.append("交付日期由不满足转为满足截止要求")
    if added:
        parts.append(f"关键路径新增 {len(added)} 个任务（{'、'.join(added[:3])}）")
    if removed:
        parts.append(f"关键路径移除 {len(removed)} 个任务")
    if new_risks:
        parts.append(f"新增 {len(new_risks)} 条风险")
    if resolved_risks:
        parts.append(f"解除 {len(resolved_risks)} 条风险")
    applied = [edit for edit in edits if edit.applied]
    if applied:
        parts.append(f"已应用 {len(applied)} 条任务扰动")
    failed = [edit for edit in edits if not edit.applied]
    if failed:
        parts.append(f"{len(failed)} 条任务扰动未能应用（见逐条原因）")
    return "；".join(parts) + "。"


__all__ = ["run_whatif"]

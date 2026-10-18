"""计划分析层（纯函数，无 IO、无状态、无算法重写）。

本模块只做两件事：

1. **把引擎已经算出来的事实做算术聚合**（进度比例、指标差值、日期偏移、偏差分桶）；
2. **按稳定文本标记给引擎风险分类**（人员过载 / 关键路径延期 / 孤立任务 / 循环依赖）。

严格边界：Kahn 拓扑排序、AOE 关键路径、工时缓冲修正、风险识别规则**全部在 TypeScript
引擎**，本模块不重新实现、不近似替代、不修改任何引擎字段语义。所有输入都来自引擎返回值，
所有输出都标注为「派生指标」，用于告警与复盘展示，不作为算法事实来源回灌引擎。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from ..constants import RETRO_CALIBRATION_BUCKETS

#: 引擎风险描述中的稳定标记 → 信号名（引擎 `src/service/risk.ts` 的文案前缀，
#: 仅用于「分类展示」，不是重新判定风险）。
RISK_MARKERS: dict[str, str] = {
    "检测到循环依赖": "cycle",
    "单日工时超限": "overload",
    "关键路径上存在 DELAYED 任务": "critical_delay",
    "孤立任务": "isolated",
}

#: 任务状态枚举（与引擎 `TASK_STATUS_VALUES` 一致，仅用于统计展示）
STATUS_DONE = "DONE"
STATUS_DELAYED = "DELAYED"
STATUS_BLOCKED = "BLOCKED"


def as_float(value: Any) -> float:
    """尽力转 float（非数值 / None → 0.0）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_date(value: Any) -> date | None:
    """解析 ``YYYY-MM-DD``（含 ISO 时间戳前缀）；非法返回 ``None``。"""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def planned_end_date(plan: dict[str, Any]) -> str | None:
    """引擎推算的计划完成日期：燃尽数据末点的 ``time``（引擎输出，不做二次推算）。"""
    burndown = plan.get("burndownRawData") or []
    if not isinstance(burndown, list) or not burndown:
        return None
    last = burndown[-1]
    time = last.get("time") if isinstance(last, dict) else None
    return str(time) if time else None


def day_offset(later: str | None, earlier: str | None) -> int | None:
    """``later - earlier`` 的自然日差；任一日期不可解析时返回 ``None``。"""
    left, right = parse_date(later), parse_date(earlier)
    if left is None or right is None:
        return None
    return (left - right).days


def deadline_met(plan: dict[str, Any]) -> bool | None:
    """计划完成日期是否不晚于截止日期（ISO 字符串按字典序比较即可）。"""
    end = planned_end_date(plan)
    deadline = plan.get("iterationDeadline")
    if not end or not deadline:
        return None
    return end <= str(deadline)


def task_map(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``taskId → 任务`` 映射（仅保留 dict 元素）。"""
    out: dict[str, dict[str, Any]] = {}
    for task in plan.get("taskList") or []:
        if isinstance(task, dict) and isinstance(task.get("taskId"), str):
            out[task["taskId"]] = task
    return out


def task_names(plan: dict[str, Any], task_ids: list[str]) -> list[str]:
    """把 taskId 列表翻译为任务名列表（找不到时回落为原 ID）。"""
    mapping = task_map(plan)
    return [str(mapping[task_id].get("taskName") or task_id) if task_id in mapping else str(task_id) for task_id in task_ids]


def risk_categories(plan: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """按引擎风险描述里的稳定标记把风险分组（未命中标记的归入 ``semantic``）。"""
    grouped: dict[str, list[dict[str, Any]]] = {name: [] for name in RISK_MARKERS.values()}
    grouped["semantic"] = []
    for risk in plan.get("riskList") or []:
        if not isinstance(risk, dict):
            continue
        description = str(risk.get("description") or "")
        bucket = next((name for marker, name in RISK_MARKERS.items() if marker in description), "semantic")
        grouped[bucket].append(risk)
    return grouped


def critical_path(plan: dict[str, Any]) -> list[str]:
    """引擎 AOE 关键路径（有序 taskId 数组）的原始副本。"""
    return [str(item) for item in (plan.get("criticalPath") or []) if item]


# ------------------------------ 计划指标（供对比） ------------------------------


def plan_metrics(plan: dict[str, Any]) -> dict[str, Any]:
    """汇总一份计划的对比指标（全部取自引擎返回值，不做算法推导）。"""
    tasks = [task for task in (plan.get("taskList") or []) if isinstance(task, dict)]
    risks = [risk for risk in (plan.get("riskList") or []) if isinstance(risk, dict)]
    return {
        "totalDurationHours": as_float(plan.get("totalDurationHours")),
        "totalEstimatedHours": as_float(plan.get("totalEstimatedHours")),
        "taskCount": len(tasks),
        "criticalTaskCount": len(critical_path(plan)),
        "riskCount": len(risks),
        "highRiskCount": sum(1 for risk in risks if str(risk.get("level")).upper() == "HIGH"),
        "plannedEndDate": planned_end_date(plan),
        "deadline": str(plan.get("iterationDeadline")) if plan.get("iterationDeadline") else None,
        "deadlineMet": deadline_met(plan),
    }


def metrics_delta(baseline: dict[str, Any], scenario: dict[str, Any]) -> dict[str, Any]:
    """情景 − 基线（仅数值字段做差；日期 / 布尔字段由调用方单独展示）。"""
    return {
        "totalDurationHours": round(as_float(scenario.get("totalDurationHours")) - as_float(baseline.get("totalDurationHours")), 4),
        "totalEstimatedHours": round(
            as_float(scenario.get("totalEstimatedHours")) - as_float(baseline.get("totalEstimatedHours")), 4
        ),
        "taskCount": int(scenario.get("taskCount") or 0) - int(baseline.get("taskCount") or 0),
        "criticalTaskCount": int(scenario.get("criticalTaskCount") or 0) - int(baseline.get("criticalTaskCount") or 0),
        "riskCount": int(scenario.get("riskCount") or 0) - int(baseline.get("riskCount") or 0),
        "highRiskCount": int(scenario.get("highRiskCount") or 0) - int(baseline.get("highRiskCount") or 0),
    }


# ------------------------------ 进度 / 交付（巡检与复盘共用） ------------------------------


def progress_stats(plan: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    """进度统计（派生指标：时间进度 vs 工时完成比例）。

    口径（全部来自引擎事实，README 有说明）：

    - ``elapsedRatio`` = (今天 − 起始日) / (截止日 − 起始日)，裁剪到 ``[0, 1]``；
    - ``doneRatio`` = 已完成任务工时 / 全部任务工时；
    - ``driftDays`` = ``(elapsedRatio − doneRatio) × 迭代自然日数``，正值表示**落后**。

    该指标只用于「要不要提醒」的判断，**不写入引擎、不影响任何排期**。
    """
    current = today or date.today()
    tasks = [task for task in (plan.get("taskList") or []) if isinstance(task, dict)]
    total_hours = sum(as_float(task.get("estimatedHours")) for task in tasks)
    done_hours = sum(
        as_float(task.get("estimatedHours")) for task in tasks if str(task.get("status")) == STATUS_DONE
    )
    done_ratio = round(done_hours / total_hours, 4) if total_hours > 0 else None
    start = parse_date(plan.get("iterationStartDate"))
    end = parse_date(plan.get("iterationDeadline"))
    elapsed_ratio: float | None = None
    drift_days: int | None = None
    if start and end and end > start:
        span = (end - start).days
        elapsed_ratio = round(min(1.0, max(0.0, (current - start).days / span)), 4)
        if done_ratio is not None:
            drift_days = int(round((elapsed_ratio - done_ratio) * span))
    status_counts: dict[str, int] = {}
    for task in tasks:
        status = str(task.get("status") or "UNKNOWN")
        status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "taskCount": len(tasks),
        "totalEstimatedHours": round(total_hours, 4),
        "doneEstimatedHours": round(done_hours, 4),
        "doneRatio": done_ratio,
        "elapsedRatio": elapsed_ratio,
        "driftDays": drift_days,
        "statusCounts": status_counts,
        "delayedTasks": [str(task.get("taskName") or task.get("taskId")) for task in tasks if str(task.get("status")) == STATUS_DELAYED],
        "blockedTasks": [str(task.get("taskName") or task.get("taskId")) for task in tasks if str(task.get("status")) == STATUS_BLOCKED],
        "unassignedCriticalTasks": [
            str(task.get("taskName") or task.get("taskId"))
            for task in tasks
            if task.get("taskId") in set(critical_path(plan)) and not str(task.get("assignee") or "").strip()
        ],
        "plannedEndDate": planned_end_date(plan),
        "deadlineMet": deadline_met(plan),
        "deadlineOffsetDays": day_offset(planned_end_date(plan), plan.get("iterationDeadline")),
    }


# ------------------------------ critic 角色的确定性核对 ------------------------------


def acceptance_gaps(plan: dict[str, Any], acceptance: list[str]) -> list[str]:
    """把引擎任务与验收点做**字面覆盖核对**（不做语义推断，语义缺口交给 LLM 复核）。

    口径：验收点去掉标点后的关键词若未出现在任何任务名 / 任务描述中，则记为「未直接承接」，
    提示人工确认；这是**提示性**结果，不判定计划不合格。
    """
    haystack = " ".join(
        f"{task.get('taskName') or ''} {task.get('taskDesc') or ''}"
        for task in (plan.get("taskList") or [])
        if isinstance(task, dict)
    )
    gaps: list[str] = []
    for item in acceptance:
        text = str(item).strip()
        if not text:
            continue
        keywords = [chunk for chunk in _split_keywords(text) if len(chunk) >= 2]
        if keywords and not any(keyword in haystack for keyword in keywords):
            gaps.append(f"验收点「{text}」未在任务名 / 描述中找到直接承接，请人工确认")
    return gaps


def _split_keywords(text: str) -> list[str]:
    """切分关键词（不引入分词依赖，纯字符级切分）。

    中文按 **2 字滑窗二元组**切分（如「下单接口可用」→ 下单 / 单接 / 接口 / 口可 / 可用），
    否则整句中文会成为单一关键词，任何任务名都匹配不上，核对必然全判为缺口；
    英文与数字按连续字符切分。
    """
    out: list[str] = []
    buffer: list[str] = []
    for char in text:
        if "\u4e00" <= char <= "\u9fff" or char.isalnum():
            buffer.append(char)
        else:
            out.extend(_chunk_keywords("".join(buffer)))
            buffer = []
    out.extend(_chunk_keywords("".join(buffer)))
    return out


def _chunk_keywords(chunk: str) -> list[str]:
    """把一段连续关键词文本转为待匹配词元（中文取二元组，其它取整体）。"""
    if not chunk:
        return []
    if all("\u4e00" <= char <= "\u9fff" for char in chunk) and len(chunk) > 2:
        return [chunk[index : index + 2] for index in range(len(chunk) - 1)]
    return [chunk]


# ------------------------------ 复盘统计 ------------------------------


def calibration_buckets(ratios: list[float]) -> dict[str, int]:
    """估算偏差分桶：``实际 / 预估`` 比值落入各区间的事件数。"""
    counts = {label: 0 for label, _low, _high in RETRO_CALIBRATION_BUCKETS}
    for ratio in ratios:
        for label, low, high in RETRO_CALIBRATION_BUCKETS:
            if low <= ratio < high:
                counts[label] += 1
                break
    return counts


def retro_metrics(
    plan: dict[str, Any],
    *,
    actual_hours: dict[str, float] | None = None,
    materialized_risk_ids: list[str] | None = None,
    scope_changes: list[str] | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """复盘统计（确定性、可复算）。

    - **工时偏差与估算校准**：需要 ``actualHours``（实际工时是外部事实，引擎计划里没有该字段，
      因此只能由调用方提供；未提供时 ``calibration.available=false`` 并说明原因，不会编造数字）；
    - **风险兑现率**：需要 ``materializedRiskIds``（人工判定「确实发生了」的风险），
      同时给出**可推断的候选兑现**（关联任务全部为 DELAYED / BLOCKED 的高风险项）供参考；
    - **交付偏移 / 范围变更 / 进度**：完全由引擎事实与配置统计得出。
    """
    tasks = [task for task in (plan.get("taskList") or []) if isinstance(task, dict)]
    by_id = task_map(plan)
    normalized_actuals: dict[str, Any] = {}
    for key, value in (actual_hours or {}).items():
        target = by_id.get(str(key))
        if target is None:
            target = next(
                (task for task in tasks if str(task.get("taskName") or "").strip() == str(key).strip()),
                None,
            )
        if target is None:
            continue
        normalized_actuals[str(target.get("taskName") or target.get("taskId"))] = as_float(value)

    deviations: list[dict[str, Any]] = []
    ratios: list[float] = []
    for name, actual in normalized_actuals.items():
        estimate = next(
            (
                as_float(task.get("estimatedHours"))
                for task in tasks
                if str(task.get("taskName") or "") == name
            ),
            0.0,
        )
        if estimate <= 0:
            continue
        ratio = round(actual / estimate, 4)
        ratios.append(ratio)
        deviations.append(
            {
                "task": name,
                "estimateHours": estimate,
                "actualHours": actual,
                "deltaHours": round(actual - estimate, 4),
                "ratio": ratio,
            }
        )
    average_ratio = round(sum(ratios) / len(ratios), 4) if ratios else None

    risks = [risk for risk in (plan.get("riskList") or []) if isinstance(risk, dict)]
    materialized = [str(item) for item in (materialized_risk_ids or [])]
    inferred: list[str] = []
    for risk in risks:
        task_ids = [str(item) for item in (risk.get("taskIds") or [])]
        if not task_ids:
            continue
        related = [by_id.get(task_id) for task_id in task_ids]
        if all(task is not None and str(task.get("status")) in (STATUS_DELAYED, STATUS_BLOCKED) for task in related):
            if str(risk.get("level")).upper() == "HIGH":
                inferred.append(str(risk.get("riskId") or ""))
    realizes_rate = round(len(materialized) / len(risks), 4) if risks and materialized else None

    return {
        "generatedAt": (today or date.today()).isoformat(),
        "progress": progress_stats(plan, today),
        "delivery": {
            "deadline": plan.get("iterationDeadline"),
            "plannedEndDate": planned_end_date(plan),
            "deadlineMet": deadline_met(plan),
            "offsetDays": day_offset(planned_end_date(plan), plan.get("iterationDeadline")),
            "totalDurationHours": as_float(plan.get("totalDurationHours")),
            "devMemberCount": plan.get("devMemberCount"),
        },
        "effort": {
            "available": bool(deviations),
            "reason": None
            if deviations
            else "未提供实际工时（引擎计划中不含实际工时字段），无法计算工时偏差与估算校准曲线",
            "averageRatio": average_ratio,
            "deviations": sorted(deviations, key=lambda item: item["ratio"], reverse=True),
            "calibration": calibration_buckets(ratios),
        },
        "risks": {
            "total": len(risks),
            "high": sum(1 for risk in risks if str(risk.get("level")).upper() == "HIGH"),
            "materializedCount": len(materialized) if materialized else None,
            "materializedRate": realizes_rate,
            "materializedRiskIds": materialized,
            "inferredMaterializedRiskIds": inferred,
            "open": [
                {
                    "riskId": risk.get("riskId"),
                    "level": risk.get("level"),
                    "description": risk.get("description"),
                }
                for risk in risks
                if str(risk.get("riskId") or "") not in set(materialized)
            ],
        },
        "scope": {
            "taskCount": len(tasks),
            "changes": [str(item) for item in (scope_changes or [])],
            "changeCount": len(scope_changes or []),
        },
    }


__all__ = [
    "RISK_MARKERS",
    "STATUS_DONE",
    "STATUS_DELAYED",
    "STATUS_BLOCKED",
    "as_float",
    "parse_date",
    "planned_end_date",
    "day_offset",
    "deadline_met",
    "task_map",
    "task_names",
    "risk_categories",
    "critical_path",
    "plan_metrics",
    "metrics_delta",
    "progress_stats",
    "acceptance_gaps",
    "calibration_buckets",
    "retro_metrics",
]

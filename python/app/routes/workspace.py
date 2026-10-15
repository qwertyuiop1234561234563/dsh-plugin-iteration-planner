"""治理端点（``/api/workspace/**``）：数据导入 / 基线快照 / 人工风险登记册 /
人员负荷 / 燃尽分析 / 任务变更历史 / 交付质量 / 配置备份。

定位与边界（严格遵守项目硬约束）：
- **不复制任何算法**。关键路径、工期、排期、风险规则、工作日规则一律以 TypeScript 引擎
  ``src/**`` 的计算结果为准；本模块只做「引擎返回值的只读聚合 + 治理数据的持久化」；
- 一切写操作（导入手工行、基线快照取数）都通过引擎既有 REST 接口完成，Python 只做
  校验编排与代理，保证算法单一事实来源不漂移；
- 审计重建类指标（实际燃尽 / 阻塞时长 / 变更历史）以 ``GET /api/audit`` 的既有记录
  为唯一输入，属于**展示性重建**，不参与引擎的排期与风险判定。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from ..constants import (
    DEFAULT_PROFILE,
    IMPORT_MODE_APPEND,
    IMPORT_MODE_NEW,
    IMPORT_MODES,
    IMPORT_NAME_MAX_LEN,
    ISSUE_HIGH_SEVERITIES,
    ISSUE_OPEN_STATUSES,
    ISSUE_PROBABILITIES,
    ISSUE_SEVERITIES,
    ISSUE_STATUSES,
    MAX_PREFERENCE_BYTES,
    WORK_HOURS_PER_DAY,
)
from ..errors import ApiError, bad_gateway, bad_request
from ..models import (
    BaselineCreateRequest,
    ImportRequest,
    ImportRow,
    IssueCreateRequest,
    IssueUpdateRequest,
    PreferencesRequest,
)
from ..state import AppContext

logger = logging.getLogger("iteration_planner.workspace")

#: 审计动作名（与引擎服务层 `audit.trace(action, ...)` 一致，集中登记避免散落字符串）
ACTION_TASK_ADD = "task_add"
ACTION_TASK_UPDATE = "task_update"
ACTION_TASK_DELETE = "task_delete"
ACTION_REPLAN = "replan_all"
ACTION_PLAN_ITERATION = "plan_iteration"

#: 会被审计重建使用的动作集合
REBUILD_ACTIONS = (ACTION_TASK_ADD, ACTION_TASK_UPDATE, ACTION_TASK_DELETE, ACTION_REPLAN, ACTION_PLAN_ITERATION)

#: 任务级变更历史关注的字段（与引擎可更新字段白名单一致，另含创建事件）
TRACKED_FIELDS = ("estimatedHours", "status", "priority", "assignee", "predecessorTaskIds")

#: 基线偏差对比的标量指标（键名与计划快照字段一致）
DIFF_SCALARS = (
    ("totalDurationHours", "关键工期(h)"),
    ("totalEstimatedHours", "总工时(h)"),
    ("devMemberCount", "开发人数"),
)


# -------------------------------------------------------------------------- #
#                                 通用工具                                    #
# -------------------------------------------------------------------------- #


def ok(data: Any) -> dict[str, Any]:
    """统一成功信封（与引擎 ``{data}`` 契约一致）。"""
    return {"data": data}


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


async def _plan(ctx: AppContext, plan_id: str) -> dict[str, Any]:
    """读取引擎计划快照（不存在时引擎直接返回 404，本层不重复判定）。"""
    plan = await ctx.engine.request("GET", f"/plans/{plan_id}")
    if not isinstance(plan, dict):
        raise bad_request("引擎返回的计划快照格式异常")
    return plan


async def _load_audit(ctx: AppContext, plan_id: str, limit: int = 1000) -> list[dict[str, Any]]:
    """读取并按时间升序整理审计记录（引擎默认按新→旧返回，此处统一为旧→新）。"""
    records = await ctx.engine.request("GET", "/audit", params={"planId": plan_id, "limit": limit})
    items = [item for item in (records or []) if isinstance(item, dict)]
    items.sort(key=lambda item: str(item.get("ts") or ""))
    return items


def _output_text(record: dict[str, Any]) -> str:
    """把审计出参统一转为文本，便于对**被截断**的 JSON 做配平提取。"""
    raw = record.get("output")
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    try:
        return json.dumps(raw, ensure_ascii=False)
    except (TypeError, ValueError):
        return ""


def _find_json(text: str, key: str) -> Any | None:
    """在被截断的 JSON 文本中提取指定键的**首个完整 JSON 值**。

    背景：引擎审计出参超过 4000 字符会被截断成字符串，无法整体 parse；但
    ``{"task": {...}, ...}`` / ``{"taskList": [...], ...}`` 这类结构中关键对象位于最前，
    通过花括号 / 方括号配平即可精确切出完整片段（与前端 analytics.ts 的既有口径一致）。
    """
    if not text:
        return None
    marker = f'"{key}"'
    start = text.find(marker)
    if start < 0:
        return None
    colon = text.find(":", start + len(marker))
    if colon < 0:
        return None
    index = colon + 1
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    if index >= len(text):
        return None
    opener = text[index]
    if opener not in "{[":
        # 标量值（字符串 / 数字 / null）：切到下一个分隔符
        end = index
        while end < len(text) and text[end] not in ",}]":
            end += 1
        raw = text[index:end].strip()
        try:
            return json.loads(raw)
        except ValueError:
            return None
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_string = False
    escaped = False
    for pos in range(index, len(text)):
        char = text[pos]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[index : pos + 1])
                except ValueError:
                    return None
    return None


def _task_from_output(record: dict[str, Any]) -> dict[str, Any] | None:
    """从审计出参中提取单个任务对象（task_update / task_add 的产物）。"""
    value = _find_json(_output_text(record), "task")
    if isinstance(value, dict) and isinstance(value.get("taskId"), str):
        return value
    return None


def _task_list_from_output(record: dict[str, Any]) -> list[dict[str, Any]] | None:
    """从审计出参中提取任务清单（task_add / replan_all 的产物；截断时可能不完整）。"""
    value = _find_json(_output_text(record), "taskList")
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict) and isinstance(item.get("taskId"), str)]
    # replan_all / plan_iteration 的出参本身就是计划对象
    raw = record.get("output")
    if isinstance(raw, dict):
        tasks = raw.get("taskList")
        if isinstance(tasks, list):
            return [item for item in tasks if isinstance(item, dict) and isinstance(item.get("taskId"), str)]
    return None


def _planned_end_date(plan: dict[str, Any]) -> str | None:
    """引擎燃尽数据末点即计划完成日期（不在本层重算日程）。"""
    burndown = plan.get("burndownRawData") or []
    if not burndown or not isinstance(burndown[-1], dict):
        return None
    tail = burndown[-1].get("time")
    return str(tail) if tail else None


def _criticals(plan: dict[str, Any]) -> list[str]:
    path = plan.get("criticalPath") or []
    return [str(item) for item in path]


def _risk_counts(plan: dict[str, Any]) -> tuple[int, int]:
    risks = [risk for risk in (plan.get("riskList") or []) if isinstance(risk, dict)]
    high = sum(1 for risk in risks if str(risk.get("level")).upper() == "HIGH")
    return len(risks), high


def _summarize(plan: dict[str, Any]) -> dict[str, Any]:
    """计划快照 → 标量摘要（用于基线列表 / 度量对比的展示口径）。"""
    tasks = [task for task in (plan.get("taskList") or []) if isinstance(task, dict)]
    risk_count, high_risk = _risk_counts(plan)
    return {
        "taskCount": len(tasks),
        "criticalTaskCount": len(_criticals(plan)),
        "riskCount": risk_count,
        "highRiskCount": high_risk,
        "totalEstimatedHours": _as_float(plan.get("totalEstimatedHours")),
        "totalDurationHours": _as_float(plan.get("totalDurationHours")),
        "plannedEndDate": _planned_end_date(plan),
    }


def _status_counts(tasks: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for task in tasks:
        status = str(task.get("status") or "TODO")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _remaining_hours(tasks: list[dict[str, Any]]) -> float:
    """剩余工时 = 未完成任务（status != DONE）的预估工时之和。"""
    total = 0.0
    for task in tasks:
        if str(task.get("status")) == "DONE":
            continue
        total += _as_float(task.get("estimatedHours"))
    return round(total, 4)


# -------------------------------------------------------------------------- #
#                                  数据导入                                   #
# -------------------------------------------------------------------------- #


async def _import_enums(ctx: AppContext) -> dict[str, set[str]]:
    """读取引擎枚举（唯一事实来源），用于导入行的取值校验。"""
    meta = await ctx.engine.request("GET", "/meta/enums")
    if not isinstance(meta, dict):
        # 引擎返回异常属于上游问题（网关视角为坏网关），不是客户端入参错误
        raise bad_gateway("无法读取引擎枚举元数据，导入校验中止")
    return {
        "taskType": {str(item) for item in (meta.get("taskType") or [])},
        "status": {str(item) for item in (meta.get("taskStatus") or [])},
        "priority": {str(item) for item in (meta.get("priority") or [])},
    }


def _validate_import(payload: ImportRequest, existing_ids: set[str], enums: dict[str, set[str]]) -> list[dict[str, Any]]:
    """导入行预校验：名称 / 枚举 / 工时 / 前置引用 / 批内重名。返回行级错误列表。

    说明：这里只做**数据校验**（形态与引用完整性），不涉及任何图算法 ——
    环检测由引擎在每次新增任务后的标准重规划中完成。
    """
    errors: list[dict[str, str]] = []
    name_index: dict[str, int] = {}
    names = [(row.taskName or "").strip() for row in payload.rows]

    for position, name in enumerate(names):
        if not name:
            errors.append({"row": position + 1, "field": "taskName", "message": "任务名称不能为空"})
            continue
        if len(name) > IMPORT_NAME_MAX_LEN:
            errors.append({"row": position + 1, "field": "taskName", "message": f"任务名称不得超过 {IMPORT_NAME_MAX_LEN} 字符"})
            continue
        if name in name_index:
            errors.append({"row": position + 1, "field": "taskName", "message": f"任务名称与第 {name_index[name] + 1} 行重复：{name}"})
            continue
        name_index[name] = position

    for position, row in enumerate(payload.rows):
        label = position + 1
        if row.taskType and row.taskType not in enums["taskType"]:
            errors.append({"row": label, "field": "taskType", "message": f"任务类型非法：{row.taskType}；可选值：{' / '.join(sorted(enums['taskType']))}"})
        if row.status and row.status not in enums["status"]:
            errors.append({"row": label, "field": "status", "message": f"任务状态非法：{row.status}；可选值：{' / '.join(sorted(enums['status']))}"})
        if row.priority and row.priority not in enums["priority"]:
            errors.append({"row": label, "field": "priority", "message": f"优先级非法：{row.priority}；可选值：{' / '.join(sorted(enums['priority']))}"})
        if row.estimatedHours is not None and row.estimatedHours <= 0:
            errors.append({"row": label, "field": "estimatedHours", "message": "预估工时必须为正数"})
        for ref in row.predecessorRefs:
            if not isinstance(ref, str) or not ref.strip():
                errors.append({"row": label, "field": "predecessorRefs", "message": "前置引用不能为空字符串"})
                continue
            target = ref.strip()
            if target in existing_ids:
                continue
            earlier = name_index.get(target)
            if earlier is None:
                errors.append({"row": label, "field": "predecessorRefs", "message": f"前置引用无法解析：{target}（须为已存在任务的 taskId 或本批次中更早的任务名称）"})
                continue
            if earlier >= position:
                errors.append({"row": label, "field": "predecessorRefs", "message": f"前置引用必须指向本批次中更早的行：{target}"})
    return errors


def _import_draft(row: ImportRow, predecessors: list[str]) -> dict[str, Any]:
    """导入行 → 引擎 addTask 入参（字段逐一映射，不做任何默认值推断）。"""
    draft: dict[str, Any] = {"taskName": (row.taskName or "").strip()}
    if row.taskDesc:
        draft["taskDesc"] = row.taskDesc
    if row.taskType:
        draft["taskType"] = row.taskType
    if row.estimatedHours is not None:
        draft["estimatedHours"] = row.estimatedHours
    if row.status:
        draft["status"] = row.status
    if row.priority:
        draft["priority"] = row.priority
    if row.assignee:
        draft["assignee"] = row.assignee
    if predecessors:
        draft["predecessorTaskIds"] = predecessors
    return draft


async def _rollback_import(ctx: AppContext, plan_id: str, created_ids: list[str]) -> int:
    """导入失败时尽力回滚已创建的任务（逆序删除，失败仅告警不掩盖主错误）。"""
    rolled = 0
    for task_id in reversed(created_ids):
        try:
            await ctx.engine.request("DELETE", f"/plans/{plan_id}/tasks/{task_id}")
            rolled += 1
        except ApiError as exc:
            logger.warning("导入回滚删除任务失败（planId=%s, taskId=%s）：%s", plan_id, task_id, exc.message)
        except Exception:  # noqa: BLE001 - 回滚失败不应影响主流程
            logger.exception("导入回滚出现未预期异常（planId=%s, taskId=%s）", plan_id, task_id)
    return rolled


async def _run_import(ctx: AppContext, payload: ImportRequest) -> dict[str, Any]:
    """执行导入：预校验 → 逐行落引擎 → 失败即回滚，保证「要么全成、要么不留半成品」。"""
    if payload.mode not in IMPORT_MODES:
        raise bad_request(f"导入模式非法：{payload.mode}")

    enums = await _import_enums(ctx)

    # 目标计划与既有任务（append 模式）；new 模式先建计划
    created_plan: dict[str, Any] | None = None
    if payload.mode == IMPORT_MODE_APPEND:
        if not payload.planId:
            raise bad_request("append 模式必须提供 planId")
        plan = await _plan(ctx, payload.planId)
        plan_id = payload.planId
    else:
        missing = [
            name
            for name, value in (
                ("requirement", payload.requirement),
                ("iterationDeadline", payload.iterationDeadline),
                ("devMemberCount", payload.devMemberCount),
            )
            if not value
        ]
        if missing:
            raise bad_request(f"new 模式缺少必填项：{'、'.join(missing)}")
        created_plan = await ctx.engine.request(
            "POST",
            "/plans",
            json={
                "requirement": payload.requirement,
                "iterationDeadline": payload.iterationDeadline,
                "iterationStartDate": payload.iterationStartDate or payload.iterationDeadline,
                "devMemberCount": payload.devMemberCount,
            },
        )
        if not isinstance(created_plan, dict) or not created_plan.get("planId"):
            raise bad_request("引擎未能返回新建计划，导入中止")
        plan_id = str(created_plan["planId"])
        plan = created_plan

    existing_tasks = [task for task in (plan.get("taskList") or []) if isinstance(task, dict)]
    existing_ids = {str(task.get("taskId")) for task in existing_tasks}

    errors = _validate_import(payload, existing_ids, enums)
    if errors:
        # new 模式：预校验未通过则回收临时新建的计划，避免留下空计划
        plan_removed = False
        if created_plan is not None:
            try:
                await ctx.engine.request("DELETE", f"/plans/{plan_id}")
                plan_removed = True
            except ApiError as exc:
                logger.warning("导入预校验失败后回收临时计划失败（planId=%s）：%s", plan_id, exc.message)
            except Exception:  # noqa: BLE001 - 回收失败不影响主错误提示
                logger.exception("导入预校验失败后回收临时计划出现未预期异常（planId=%s）", plan_id)
        raise bad_request(
            "导入数据预校验未通过",
            {
                "errors": errors,
                "rolledBackPlan": plan_removed,
                "planId": None if plan_removed else plan_id,
            },
        )

    # 逐行落库：前置引用解析为 taskId（既有任务或本批次已创建任务）
    resolved_by_name: dict[str, str] = {}
    created_ids: list[str] = []
    imported: list[dict[str, str]] = []
    for position, row in enumerate(payload.rows):
        predecessors: list[str] = []
        for ref in row.predecessorRefs:
            target = ref.strip()
            predecessors.append(target if target in existing_ids else resolved_by_name.get(target, target))
        try:
            result = await ctx.engine.request(
                "POST",
                f"/plans/{plan_id}/tasks",
                json=_import_draft(row, predecessors),
            )
            task = (result or {}).get("task") if isinstance(result, dict) else None
            if not isinstance(task, dict) or not task.get("taskId"):
                raise bad_request("引擎新增任务未返回任务实体")
        except Exception as exc:  # noqa: BLE001 - 任何异常都必须回滚，避免留下半成品
            # 除 ApiError 外还可能是传输层异常 / 响应体解析异常；统一回滚本次已创建的任务，
            # 并把原始异常统一转换为错误工具结果返回，不中断整个编排流程。
            message = exc.message if isinstance(exc, ApiError) else f"{type(exc).__name__}: {exc}"
            rolled = await _rollback_import(ctx, plan_id, created_ids)
            # new 模式：导入不完整则回收整个计划，避免留下半成品计划
            plan_removed = False
            if created_plan is not None:
                try:
                    await ctx.engine.request("DELETE", f"/plans/{plan_id}")
                    plan_removed = True
                except ApiError as cleanup_exc:
                    logger.warning("导入失败后回收临时计划失败（planId=%s）：%s", plan_id, cleanup_exc.message)
                except Exception:  # noqa: BLE001 - 回收失败不影响主错误提示
                    logger.exception("导入失败后回收临时计划出现未预期异常（planId=%s）", plan_id)
            raise bad_request(
                f"第 {position + 1} 行导入失败，已回滚本次导入（回滚 {rolled} 个任务）：{message}",
                {
                    "row": position + 1,
                    "taskName": row.taskName,
                    "rolledBackTasks": rolled,
                    "rolledBackPlan": plan_removed,
                    "planId": None if plan_removed else plan_id,
                },
            ) from exc

        task_id = str(task["taskId"])
        created_ids.append(task_id)
        resolved_by_name[(row.taskName or "").strip()] = task_id
        imported.append({"ref": (row.taskName or "").strip(), "taskId": task_id, "taskName": str(task.get("taskName") or "")})

    return {
        "planId": plan_id,
        "mode": payload.mode,
        "addedCount": len(imported),
        "tasks": imported,
        "createdPlan": payload.mode == IMPORT_MODE_NEW,
    }


# -------------------------------------------------------------------------- #
#                             基线快照与偏差对比                              #
# -------------------------------------------------------------------------- #


def _baseline_summary(record: dict[str, Any]) -> dict[str, Any]:
    snapshot = record.get("snapshot") or {}
    return {
        "baselineId": record.get("baselineId"),
        "name": record.get("name"),
        "note": record.get("note"),
        "createdAt": record.get("createdAt"),
        "planId": record.get("planId"),
        **_summarize(snapshot if isinstance(snapshot, dict) else {}),
    }


def _scalar_diff(before: float | str | None, after: float | str | None, numeric: bool) -> dict[str, Any]:
    if numeric:
        left = _as_float(before)
        right = _as_float(after)
        return {"from": left, "to": right, "delta": round(right - left, 4)}
    return {"from": before, "to": after, "delta": None}


def _task_diff(baseline_tasks: list[dict[str, Any]], current_tasks: list[dict[str, Any]]) -> dict[str, Any]:
    """基线 → 现状的任务级偏差：新增 / 删除 / 字段变更（纯比对，不含算法）。"""
    baseline_index = {str(task.get("taskId")): task for task in baseline_tasks}
    current_index = {str(task.get("taskId")): task for task in current_tasks}

    added = [
        {
            "taskId": task_id,
            "taskName": current_index[task_id].get("taskName"),
            "estimatedHours": _as_float(current_index[task_id].get("estimatedHours")),
            "status": current_index[task_id].get("status"),
            "priority": current_index[task_id].get("priority"),
        }
        for task_id in current_index
        if task_id not in baseline_index
    ]
    removed = [
        {
            "taskId": task_id,
            "taskName": baseline_index[task_id].get("taskName"),
            "estimatedHours": _as_float(baseline_index[task_id].get("estimatedHours")),
            "status": baseline_index[task_id].get("status"),
            "priority": baseline_index[task_id].get("priority"),
        }
        for task_id in baseline_index
        if task_id not in current_index
    ]

    changed = []
    for task_id, before in baseline_index.items():
        after = current_index.get(task_id)
        if after is None:
            continue
        fields = []
        for field in ("estimatedHours", "status", "priority", "assignee", "predecessorTaskIds"):
            left = before.get(field)
            right = after.get(field)
            if field == "estimatedHours":
                if abs(_as_float(left) - _as_float(right)) < 1e-9:
                    continue
            elif json.dumps(left, ensure_ascii=False, sort_keys=True) == json.dumps(right, ensure_ascii=False, sort_keys=True):
                continue
            fields.append({"field": field, "from": left, "to": right})
        if fields:
            changed.append({"taskId": task_id, "taskName": after.get("taskName"), "fields": fields})
    return {"added": added, "removed": removed, "changed": changed}


async def _baseline_diff(ctx: AppContext, plan_id: str, baseline_id: str) -> dict[str, Any]:
    store = ctx.workspace
    record = await store.get_baseline(plan_id, baseline_id)
    snapshot = record.get("snapshot") or {}
    current = await _plan(ctx, plan_id)

    baseline_tasks = [task for task in (snapshot.get("taskList") or []) if isinstance(task, dict)]
    current_tasks = [task for task in (current.get("taskList") or []) if isinstance(task, dict)]
    task_changes = _task_diff(baseline_tasks, current_tasks)

    before = _summarize(snapshot if isinstance(snapshot, dict) else {})
    after = _summarize(current)
    metrics = {
        "taskCount": _scalar_diff(before["taskCount"], after["taskCount"], True),
        "criticalTaskCount": _scalar_diff(before["criticalTaskCount"], after["criticalTaskCount"], True),
        "riskCount": _scalar_diff(before["riskCount"], after["riskCount"], True),
        "highRiskCount": _scalar_diff(before["highRiskCount"], after["highRiskCount"], True),
        "plannedEndDate": _scalar_diff(before["plannedEndDate"], after["plannedEndDate"], False),
    }
    for key, _label in DIFF_SCALARS:
        metrics[key] = _scalar_diff(snapshot.get(key), current.get(key), True)

    return {
        "baseline": _baseline_summary(record),
        "currentPlanId": plan_id,
        "currentCapturedAt": _now_iso(),
        **task_changes,
        "metrics": metrics,
    }


# -------------------------------------------------------------------------- #
#                          人工风险登记册（治理层）                            #
# -------------------------------------------------------------------------- #


def _validate_issue_enum(field: str, value: str, allowed: tuple[str, ...]) -> None:
    if value not in allowed:
        raise bad_request(f"{field} 取值非法：{value}；可选值：{' / '.join(allowed)}")


async def _issue_payload(payload: IssueCreateRequest, plan_id: str) -> dict[str, Any]:
    _validate_issue_enum("severity", payload.severity, ISSUE_SEVERITIES)
    _validate_issue_enum("probability", payload.probability, ISSUE_PROBABILITIES)
    _validate_issue_enum("status", payload.status, ISSUE_STATUSES)
    now = _now_iso()
    return {
        "issueId": _new_id(),
        "planId": plan_id,
        "title": payload.title,
        "description": payload.description,
        "severity": payload.severity,
        "probability": payload.probability,
        "owner": payload.owner,
        "mitigation": payload.mitigation,
        "status": payload.status,
        "dueDate": payload.dueDate,
        "linkedTaskIds": list(payload.linkedTaskIds),
        "createdAt": now,
        "updatedAt": now,
    }


async def _issue_patch(payload: IssueUpdateRequest) -> dict[str, Any]:
    patch = payload.model_dump(exclude_unset=True)
    if "severity" in patch and patch["severity"] is not None:
        _validate_issue_enum("severity", str(patch["severity"]), ISSUE_SEVERITIES)
    if "probability" in patch and patch["probability"] is not None:
        _validate_issue_enum("probability", str(patch["probability"]), ISSUE_PROBABILITIES)
    if "status" in patch and patch["status"] is not None:
        _validate_issue_enum("status", str(patch["status"]), ISSUE_STATUSES)
    patch["updatedAt"] = _now_iso()
    return patch


# -------------------------------------------------------------------------- #
#                                路由注册                                     #
# -------------------------------------------------------------------------- #


def build_workspace_router(base: str) -> APIRouter:
    """构建治理能力路由（``/api/workspace/**``）。"""
    router = APIRouter(prefix=f"{base}/workspace", tags=["workspace"])

    # ------------------------------- 数据导入 -------------------------------

    @router.post("/import", summary="数据导入：CSV/JSON 行 → 追加到现有计划或新建计划")
    async def import_tasks(payload: ImportRequest, request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        return ok(await _run_import(ctx, payload))

    # ------------------------------- 基线快照 -------------------------------

    @router.get("/plans/{plan_id}/baselines", summary="基线快照列表")
    async def list_baselines(plan_id: str, request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        records = await ctx.workspace.list_baselines(plan_id)
        items = sorted((_baseline_summary(record) for record in records), key=lambda item: str(item.get("createdAt") or ""))
        return ok({"planId": plan_id, "items": items})

    @router.post("/plans/{plan_id}/baselines", summary="创建基线快照（快照体取自引擎当前计划）")
    async def create_baseline(plan_id: str, payload: BaselineCreateRequest, request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        plan = await _plan(ctx, plan_id)
        record = {
            "baselineId": _new_id(),
            "planId": plan_id,
            "name": payload.name,
            "note": payload.note,
            "createdAt": _now_iso(),
            "snapshot": plan,
        }
        stored = await ctx.workspace.add_baseline(plan_id, record)
        return ok(_baseline_summary(stored))

    @router.get("/plans/{plan_id}/baselines/{baseline_id}", summary="基线快照详情（含任务快照）")
    async def get_baseline(plan_id: str, baseline_id: str, request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        record = await ctx.workspace.get_baseline(plan_id, baseline_id)
        snapshot = record.get("snapshot") if isinstance(record.get("snapshot"), dict) else {}
        return ok({**_baseline_summary(record), "taskList": (snapshot or {}).get("taskList") or [], "riskList": (snapshot or {}).get("riskList") or []})

    @router.delete("/plans/{plan_id}/baselines/{baseline_id}", summary="删除基线快照")
    async def delete_baseline(plan_id: str, baseline_id: str, request: Request) -> Response:
        ctx: AppContext = request.app.state.ctx
        await ctx.workspace.delete_baseline(plan_id, baseline_id)
        return Response(status_code=204)

    @router.get("/plans/{plan_id}/baselines/{baseline_id}/diff", summary="基线 vs 现状的偏差对比")
    async def baseline_diff(plan_id: str, baseline_id: str, request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        return ok(await _baseline_diff(ctx, plan_id, baseline_id))

    # ---------------------------- 人工风险登记册 ----------------------------

    @router.get("/plans/{plan_id}/registers", summary="人工风险登记册列表")
    async def list_registers(plan_id: str, request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        items = await ctx.workspace.list_issues(plan_id)
        open_items = [item for item in items if item.get("status") in ISSUE_OPEN_STATUSES]
        high_open = [item for item in open_items if item.get("severity") in ISSUE_HIGH_SEVERITIES]
        return ok(
            {
                "planId": plan_id,
                "items": items,
                "stats": {
                    "total": len(items),
                    "open": len(open_items),
                    "highOpen": len(high_open),
                    "severities": {level: sum(1 for item in items if item.get("severity") == level) for level in ISSUE_SEVERITIES},
                    "statuses": {state: sum(1 for item in items if item.get("status") == state) for state in ISSUE_STATUSES},
                },
            }
        )

    @router.post("/plans/{plan_id}/registers", summary="登记一条人工风险")
    async def create_register(plan_id: str, payload: IssueCreateRequest, request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        record = await _issue_payload(payload, plan_id)
        return ok(await ctx.workspace.add_issue(plan_id, record))

    @router.patch("/plans/{plan_id}/registers/{issue_id}", summary="更新人工风险条目（部分字段）")
    async def update_register(plan_id: str, issue_id: str, payload: IssueUpdateRequest, request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        patch = await _issue_patch(payload)
        return ok(await ctx.workspace.update_issue(plan_id, issue_id, patch))

    @router.delete("/plans/{plan_id}/registers/{issue_id}", summary="删除人工风险条目")
    async def delete_register(plan_id: str, issue_id: str, request: Request) -> Response:
        ctx: AppContext = request.app.state.ctx
        await ctx.workspace.delete_issue(plan_id, issue_id)
        return Response(status_code=204)

    # ------------------------------ 人员负荷 ------------------------------

    @router.get("/plans/{plan_id}/workload", summary="人员负荷与资源平衡（按责任人聚合）")
    async def workload(plan_id: str, request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        plan = await _plan(ctx, plan_id)
        tasks = [task for task in (plan.get("taskList") or []) if isinstance(task, dict)]
        critical = set(_criticals(plan))
        # 迭代可用工作日取自引擎燃尽数据的点数（工作日规则的唯一事实来源在引擎）
        plan_workdays = len([point for point in (plan.get("burndownRawData") or []) if isinstance(point, dict)])
        capacity_hours = round(plan_workdays * WORK_HOURS_PER_DAY, 4)

        members: dict[str, dict[str, Any]] = {}
        unassigned: list[dict[str, Any]] = []
        for task in tasks:
            assignee = str(task.get("assignee") or "").strip()
            hours = _as_float(task.get("estimatedHours"))
            is_done = str(task.get("status")) == "DONE"
            if not assignee:
                unassigned.append(
                    {
                        "taskId": task.get("taskId"),
                        "taskName": task.get("taskName"),
                        "estimatedHours": hours,
                        "priority": task.get("priority"),
                        "critical": str(task.get("taskId")) in critical,
                    }
                )
                continue
            entry = members.setdefault(
                assignee,
                {"assignee": assignee, "taskCount": 0, "criticalCount": 0, "estimatedHours": 0.0, "remainingHours": 0.0},
            )
            entry["taskCount"] += 1
            entry["estimatedHours"] = round(entry["estimatedHours"] + hours, 4)
            if str(task.get("taskId")) in critical:
                entry["criticalCount"] += 1
            if not is_done:
                entry["remainingHours"] = round(entry["remainingHours"] + hours, 4)

        rows = []
        for entry in sorted(members.values(), key=lambda item: item["estimatedHours"], reverse=True):
            ratio = round(entry["estimatedHours"] / capacity_hours, 4) if capacity_hours > 0 else 0.0
            rows.append(
                {
                    **entry,
                    "capacityHours": capacity_hours,
                    "loadRatio": ratio,
                    "overloaded": capacity_hours > 0 and entry["estimatedHours"] > capacity_hours,
                }
            )
        overloaded = [row["assignee"] for row in rows if row["overloaded"]]
        return ok(
            {
                "planId": plan_id,
                "hoursPerDay": WORK_HOURS_PER_DAY,
                "planWorkdays": plan_workdays,
                "capacityHoursPerMember": capacity_hours,
                "members": rows,
                "unassigned": {
                    "taskCount": len(unassigned),
                    "estimatedHours": round(sum(item["estimatedHours"] for item in unassigned), 4),
                    "tasks": unassigned,
                },
                "totals": {
                    "memberCount": len(rows),
                    "capacityHours": round(capacity_hours * len(rows), 4),
                    "allocatedHours": round(sum(row["estimatedHours"] for row in rows), 4),
                    "overloadedMembers": overloaded,
                },
            }
        )

    # --------------------------- 燃尽分析（增强） ---------------------------

    @router.get("/plans/{plan_id}/burndown-analysis", summary="燃尽增强：审计重建实际剩余曲线 + 偏离度 + 预计完成日")
    async def burndown_analysis(plan_id: str, request: Request, limit: int = 1000) -> dict:
        ctx: AppContext = request.app.state.ctx
        plan = await _plan(ctx, plan_id)
        ideal = [point for point in (plan.get("burndownRawData") or []) if isinstance(point, dict)]
        tasks = [task for task in (plan.get("taskList") or []) if isinstance(task, dict)]
        records = await _load_audit(ctx, plan_id, max(1, min(limit, 2000)))

        # 基线状态：有状态变迁记录的任务 → TODO（它们确实从待办开始流动）；
        # 无记录的任务 → 取当前状态（自始至终未变）。与前端 CFD 的既有口径一致。
        changed_ids = {
            str((record.get("input") or {}).get("taskId"))
            for record in records
            if record.get("ok") and record.get("action") == ACTION_TASK_UPDATE and isinstance(record.get("input"), dict)
            and (record.get("input") or {}).get("updateField") == "status"
        }
        state: dict[str, dict[str, Any]] = {}
        for task in tasks:
            task_id = str(task.get("taskId"))
            state[task_id] = {
                "status": "TODO" if task_id in changed_ids else str(task.get("status") or "TODO"),
                "estimatedHours": _as_float(task.get("estimatedHours")),
            }

        points: list[dict[str, Any]] = []
        baseline_remaining = _remaining_hours([{"status": item["status"], "estimatedHours": item["estimatedHours"]} for item in state.values()])
        if records:
            first_time = str(records[0].get("ts") or "")
            points.append({"time": first_time, "remainingHours": baseline_remaining, "statusCounts": _status_counts([{"status": item["status"]} for item in state.values()])})

        for record in records:
            if not record.get("ok"):
                continue
            action = record.get("action")
            mutated = False

            if action == ACTION_TASK_UPDATE:
                snapshot = _task_from_output(record)
                if snapshot:
                    task_id = str(snapshot.get("taskId"))
                    state[task_id] = {
                        "status": str(snapshot.get("status") or "TODO"),
                        "estimatedHours": _as_float(snapshot.get("estimatedHours")),
                    }
                    mutated = True
            elif action == ACTION_TASK_ADD:
                listed = _task_list_from_output(record)
                if listed is None:
                    single = _task_from_output(record)
                    listed = [single] if single else []
                for task in listed:
                    task_id = str(task.get("taskId"))
                    if task_id in state:
                        continue
                    state[task_id] = {
                        "status": str(task.get("status") or "TODO"),
                        "estimatedHours": _as_float(task.get("estimatedHours")),
                    }
                    mutated = True
            elif action == ACTION_TASK_DELETE:
                task_id = str((record.get("input") or {}).get("taskId") or "")
                if task_id in state:
                    state.pop(task_id, None)
                    mutated = True
            elif action in (ACTION_REPLAN, ACTION_PLAN_ITERATION):
                listed = _task_list_from_output(record)
                if listed:
                    state = {
                        str(task.get("taskId")): {
                            "status": str(task.get("status") or "TODO"),
                            "estimatedHours": _as_float(task.get("estimatedHours")),
                        }
                        for task in listed
                    }
                    mutated = True

            if not mutated:
                continue
            remaining = _remaining_hours([{"status": item["status"], "estimatedHours": item["estimatedHours"]} for item in state.values()])
            counts = _status_counts([{"status": item["status"]} for item in state.values()])
            if points and points[-1]["remainingHours"] == remaining and points[-1]["statusCounts"] == counts:
                continue
            points.append({"time": str(record.get("ts") or ""), "remainingHours": remaining, "statusCounts": counts})

        # 末端用计划的真实状态收口，保证曲线终点与现状一致
        current_remaining = _remaining_hours(tasks)
        current_counts = _status_counts(tasks)
        points.append({"time": _now_iso(), "remainingHours": current_remaining, "statusCounts": current_counts})

        analysis = _burndown_projection(points, ideal, current_remaining)
        return ok(
            {
                "planId": plan_id,
                "ideal": ideal,
                "actual": points,
                "recordCount": len(records),
                **analysis,
            }
        )

    # --------------------------- 任务变更历史 ---------------------------

    @router.get("/plans/{plan_id}/tasks/{task_id}/history", summary="任务级变更历史（由审计重建）")
    async def task_history(plan_id: str, task_id: str, request: Request, limit: int = 1000) -> dict:
        ctx: AppContext = request.app.state.ctx
        records = await _load_audit(ctx, plan_id, max(1, min(limit, 2000)))
        entries = _build_task_history(records, task_id)
        current = None
        try:
            plan = await _plan(ctx, plan_id)
            current = next((task for task in (plan.get("taskList") or []) if isinstance(task, dict) and str(task.get("taskId")) == task_id), None)
        except ApiError:
            current = None
        return ok(
            {
                "planId": plan_id,
                "taskId": task_id,
                "taskName": (current or {}).get("taskName"),
                "entries": list(reversed(entries)),
                "entryCount": len(entries),
            }
        )

    # ----------------------------- 交付质量 -----------------------------

    @router.get("/plans/{plan_id}/quality", summary="交付质量看板（引擎计划 + 审计 + 登记册聚合）")
    async def quality(plan_id: str, request: Request, limit: int = 1000) -> dict:
        ctx: AppContext = request.app.state.ctx
        plan = await _plan(ctx, plan_id)
        tasks = [task for task in (plan.get("taskList") or []) if isinstance(task, dict)]
        records = await _load_audit(ctx, plan_id, max(1, min(limit, 2000)))
        issues = await ctx.workspace.list_issues(plan_id)

        counts = _status_counts(tasks)
        total = len(tasks)
        done = counts.get("DONE", 0)
        delayed = counts.get("DELAYED", 0)
        blocked = counts.get("BLOCKED", 0)

        # 阻塞时长：以审计中「最后一次进入 BLOCKED」的时间为起点，当前仍阻塞的累计计算
        blocked_since = _last_transition(records, "status", "BLOCKED")
        now = datetime.now(timezone.utc)
        blocked_tasks = []
        for task in tasks:
            task_id = str(task.get("taskId"))
            if str(task.get("status")) != "BLOCKED":
                continue
            since = blocked_since.get(task_id)
            hours = None
            if since:
                try:
                    hours = round((now - datetime.fromisoformat(since)).total_seconds() / 3600, 2)
                except ValueError:
                    hours = None
            blocked_tasks.append({"taskId": task_id, "taskName": task.get("taskName"), "since": since, "blockedHours": hours})
        blocked_tasks.sort(key=lambda item: (-(item["blockedHours"] or 0), str(item.get("taskName") or "")))

        # 按时完成率：仅统计「有 DONE 状态变迁记录且计划完成日可知」的任务
        done_at = _last_transition(records, "status", "DONE")
        planned_end = _planned_end_date(plan)
        on_time_sample = 0
        on_time_hit = 0
        if planned_end:
            for task_id, ts in done_at.items():
                if str((next((t for t in tasks if str(t.get("taskId")) == task_id), {}) or {}).get("status")) != "DONE":
                    continue
                on_time_sample += 1
                if str(ts)[:10] <= planned_end:
                    on_time_hit += 1

        risk_count, high_risk = _risk_counts(plan)
        open_issues = [item for item in issues if item.get("status") in ISSUE_OPEN_STATUSES]
        high_open = [item for item in open_issues if item.get("severity") in ISSUE_HIGH_SEVERITIES]

        return ok(
            {
                "planId": plan_id,
                "taskTotal": total,
                "statusCounts": counts,
                "doneCount": done,
                "delayedCount": delayed,
                "blockedCount": blocked,
                "doneRate": round(done / total, 4) if total else 0.0,
                "delayedRate": round(delayed / total, 4) if total else 0.0,
                "onTimeRate": round(on_time_hit / on_time_sample, 4) if on_time_sample else None,
                "onTimeSample": on_time_sample,
                "blockedTasks": blocked_tasks,
                "totalBlockedHours": round(sum(item["blockedHours"] or 0 for item in blocked_tasks), 2),
                "engineRiskCount": risk_count,
                "engineHighRiskCount": high_risk,
                "registerTotal": len(issues),
                "registerOpenCount": len(open_issues),
                "registerHighOpenCount": len(high_open),
                "issueDensity": round(len(open_issues) / total * 100, 2) if total else 0.0,
                "plannedEndDate": planned_end,
            }
        )

    # ----------------------------- 配置备份 -----------------------------

    @router.get("/preferences", summary="读取服务端配置备份")
    async def get_preferences(request: Request, profile: str = DEFAULT_PROFILE) -> dict:
        ctx: AppContext = request.app.state.ctx
        stored = await ctx.workspace.get_preferences(profile)
        return ok({"profile": profile, "updatedAt": (stored or {}).get("updatedAt"), "payload": (stored or {}).get("payload")})

    @router.put("/preferences", summary="写入服务端配置备份")
    async def put_preferences(payload: PreferencesRequest, request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        profile = payload.profile or DEFAULT_PROFILE
        blob = {"updatedAt": _now_iso(), "payload": payload.payload}
        encoded = json.dumps(blob, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_PREFERENCE_BYTES:
            raise bad_request(f"配置备份体积超限（{len(encoded)} 字节 > {MAX_PREFERENCE_BYTES} 字节），请精简后重试")
        await ctx.workspace.put_preferences(profile, blob)
        return ok({"profile": profile, "updatedAt": blob["updatedAt"], "bytes": len(encoded)})

    @router.delete("/preferences", summary="删除服务端配置备份")
    async def delete_preferences(request: Request, profile: str = DEFAULT_PROFILE) -> Response:
        ctx: AppContext = request.app.state.ctx
        await ctx.workspace.delete_preferences(profile)
        return Response(status_code=204)

    return router


# -------------------------------------------------------------------------- #
#                            燃尽 / 变更历史 派生                              #
# -------------------------------------------------------------------------- #


def _burndown_projection(
    points: list[dict[str, Any]],
    ideal: list[dict[str, Any]],
    current_remaining: float,
) -> dict[str, Any]:
    """由实际剩余曲线推导偏离度、燃尽速率与预计完成日期（纯展示性推算）。"""
    planned_end = None
    if ideal and isinstance(ideal[-1], dict):
        planned_end = str(ideal[-1].get("time") or "") or None

    if len(points) < 2:
        return {"burnRatePerDay": None, "deviationHours": None, "projectedEndDate": None, "onTrack": None}

    first = points[0]
    last = points[-1]
    try:
        first_time = datetime.fromisoformat(str(first["time"]))
        last_time = datetime.fromisoformat(str(last["time"]))
    except (ValueError, KeyError):
        return {"burnRatePerDay": None, "deviationHours": None, "projectedEndDate": None, "onTrack": None}

    elapsed_days = (last_time - first_time).total_seconds() / 86400
    burned = _as_float(first.get("remainingHours")) - _as_float(last.get("remainingHours"))
    burn_rate = round(burned / elapsed_days, 4) if elapsed_days > 0 else None

    last_date = str(last["time"])[:10]
    ideal_at_last = None
    for point in ideal:
        if not isinstance(point, dict):
            continue
        point_time = str(point.get("time") or "")
        if point_time <= last_date:
            ideal_at_last = _as_float(point.get("remainingHours"))
        else:
            break
    if ideal_at_last is None and ideal and isinstance(ideal[-1], dict):
        ideal_at_last = _as_float(ideal[-1].get("remainingHours"))
    deviation = round(current_remaining - ideal_at_last, 4) if ideal_at_last is not None else None

    projected = None
    if current_remaining <= 0:
        projected = last_date
    elif burn_rate and burn_rate > 0:
        days = current_remaining / burn_rate
        projected = (last_time.timestamp() + days * 86400)
        projected = datetime.fromtimestamp(projected, tz=timezone.utc).date().isoformat()

    on_track = None
    if projected and planned_end:
        on_track = projected <= planned_end
    return {"burnRatePerDay": burn_rate, "deviationHours": deviation, "projectedEndDate": projected, "onTrack": on_track}


def _last_transition(records: list[dict[str, Any]], field: str, value: str) -> dict[str, str]:
    """取每个任务在指定字段上「最后一次变为 value」的时间（键为 taskId）。"""
    result: dict[str, str] = {}
    for record in records:
        if not record.get("ok") or record.get("action") != ACTION_TASK_UPDATE:
            continue
        payload = record.get("input")
        if not isinstance(payload, dict) or payload.get("updateField") != field:
            continue
        snapshot = _task_from_output(record)
        if not snapshot or str(snapshot.get(field)) != value:
            continue
        task_id = str(snapshot.get("taskId"))
        result[task_id] = str(record.get("ts") or "")
    return result


def _build_task_history(records: list[dict[str, Any]], task_id: str) -> list[dict[str, Any]]:
    """由审计重建单个任务的字段变更时间线（旧值取自同一字段的上一次已知值）。"""
    entries: list[dict[str, Any]] = []
    known_ids: set[str] = set()
    last_known: dict[str, Any] = {}
    for record in records:
        action = record.get("action")
        payload = record.get("input") if isinstance(record.get("input"), dict) else {}
        timestamp = str(record.get("ts") or "")

        if action == ACTION_TASK_ADD:
            listed = _task_list_from_output(record)
            if listed is None:
                single = _task_from_output(record)
                listed = [single] if single else []
            fresh = [task for task in listed if str(task.get("taskId")) not in known_ids]
            known_ids.update(str(task.get("taskId")) for task in listed)
            if any(str(task.get("taskId")) == task_id for task in fresh):
                snapshot = next(task for task in fresh if str(task.get("taskId")) == task_id)
                for field in TRACKED_FIELDS:
                    if field in snapshot:
                        last_known[field] = snapshot.get(field)
                entries.append(
                    {
                        "recordId": record.get("recordId"),
                        "ts": timestamp,
                        "action": ACTION_TASK_ADD,
                        "field": "taskName",
                        "from": None,
                        "to": snapshot.get("taskName") or payload.get("taskName"),
                        "ok": bool(record.get("ok")),
                        "error": record.get("error"),
                    }
                )
            continue

        if action in (ACTION_REPLAN, ACTION_PLAN_ITERATION):
            listed = _task_list_from_output(record) or []
            known_ids.update(str(task.get("taskId")) for task in listed)
            if any(str(task.get("taskId")) == task_id for task in listed):
                snapshot = next(task for task in listed if str(task.get("taskId")) == task_id)
                for field in TRACKED_FIELDS:
                    if field in snapshot and field not in last_known:
                        last_known[field] = snapshot.get(field)
            continue

        if action == ACTION_TASK_DELETE and str(payload.get("taskId") or "") == task_id:
            entries.append(
                {
                    "recordId": record.get("recordId"),
                    "ts": timestamp,
                    "action": ACTION_TASK_DELETE,
                    "field": None,
                    "from": None,
                    "to": None,
                    "ok": bool(record.get("ok")),
                    "error": record.get("error"),
                }
            )
            continue

        if action != ACTION_TASK_UPDATE:
            continue
        snapshot = _task_from_output(record)
        if not snapshot or str(snapshot.get("taskId")) != task_id:
            continue
        field = str(payload.get("updateField") or "")
        to_value = snapshot.get(field)
        entries.append(
            {
                "recordId": record.get("recordId"),
                "ts": timestamp,
                "action": ACTION_TASK_UPDATE,
                "field": field,
                "from": last_known.get(field),
                "to": to_value,
                "ok": bool(record.get("ok")),
                "error": record.get("error"),
            }
        )
        last_known[field] = to_value
    return entries


__all__ = ["build_workspace_router"]

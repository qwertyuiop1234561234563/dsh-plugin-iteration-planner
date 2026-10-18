"""多角色协作编排（一句话需求 → 需求分析 / 引擎排期 / 估算审阅 / 风险缓解 / 质量校验）。

参考 OpenHands 的 plan-act-observe 与 CrewAI 的角色分工：把一次「端到端规划」拆成**可见的步骤流**，
每个步骤由**专职角色**完成，中间结果实时下发前端（``step`` 事件），用户随时可中断（断开 SSE
连接即取消本轮，已产出的计划保留在持久化存储中）。

角色与职责边界（严格分工，避免「每个角色都各自算一遍」导致口径冲突）：

====================  ================  ======================================================
角色                  执行者            产出与算法边界
====================  ================  ======================================================
planner（需求分析）    LLM（可降级）     需求补充说明 / 验收点 / 假设 / 需人工确认的矛盾点。**不产出任何数值**。
scheduler（排期）      **算法引擎**      调用 ``plan_iteration`` 跑完整流水线（拆解→缓冲→DAG→AOE→风险→燃尽）。
                                       Kahn 拓扑 / AOE 关键路径 / 工时修正 / 风险识别全部在引擎。
estimator（估算审阅）  LLM（可降级）     基于引擎**真实**任务清单提出修正建议；修正转成**待确认动作**，
                                       经用户确认后才由引擎执行（不直接落库）。
risk（风险缓解）       LLM（可降级）     只补缓解动作，风险条目与等级一律取引擎检出结果。
critic（质量校验）     LLM + 确定性核对   验收点覆盖 / 关键路径责任人 / 孤立任务 / 交付日期，以引擎事实为准。
====================  ================  ======================================================

降级语义：任一角色不可用（未配置 LLM / 输出不合法）时**跳过该角色**，步骤状态标为 ``skipped`` /
``failed`` 并说明原因，其余角色继续；scheduler 是唯一硬依赖（失败即整轮失败）。
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import date, timedelta
from typing import Any, AsyncIterator

from ..constants import SSE_FINAL, SSE_PENDING, SSE_STEP, SSE_TOOL_RESULT, SSE_TOOL_START
from ..engine_client import EngineClient
from ..errors import ApiError
from ..metrics import METRICS
from ..models import AgentRunResult, PendingAction, ToolCallRecord
from .advisor import generate_advice
from .analytics import acceptance_gaps, critical_path, progress_stats, task_map
from .json_utils import extract_json_object
from .prompts import (
    CRITIC_ROLE_PROMPT,
    ESTIMATOR_ROLE_PROMPT,
    PLANNER_ROLE_PROMPT,
    build_critic_instruction,
    build_estimator_instruction,
    build_planner_instruction,
)
from .runner import AgentRunner
from .sanitize import sanitize_user_input
from .schemas import UPDATABLE_FIELDS, describe_action
from .tools import ToolDispatcher

logger = logging.getLogger("iteration_planner.collab")

#: 角色名（步骤流标识，与前端时间线一一对应）
ROLE_PLANNER = "planner"
ROLE_SCHEDULER = "scheduler"
ROLE_ESTIMATOR = "estimator"
ROLE_RISK = "risk"
ROLE_CRITIC = "critic"
ROLE_LABELS: dict[str, str] = {
    ROLE_PLANNER: "需求分析",
    ROLE_SCHEDULER: "排期计算（算法引擎）",
    ROLE_ESTIMATOR: "估算审阅",
    ROLE_RISK: "风险缓解",
    ROLE_CRITIC: "质量校验",
}

#: 步骤状态
STEP_RUNNING = "running"
STEP_DONE = "done"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"

#: 单个角色 LLM 输出的长度上限（防御性：超长输出必然不是合法结构化结果）
_MAX_ROLE_OUTPUT = 8000
#: 估算建议最多转为多少条待确认动作（避免一次登记过多写操作，用户无法核对）
_MAX_PENDING = 8


def _step(role: str, status: str, detail: str = "") -> dict[str, Any]:
    """构造一个步骤事件载荷。"""
    return {
        "role": role,
        "title": ROLE_LABELS.get(role, role),
        "status": status,
        "detail": detail,
        "at": time.time(),
    }


async def _role_json(runner: AgentRunner, system: str, instruction: str, *, temperature: float = 0.1) -> dict[str, Any]:
    """调用角色模型并要求返回 JSON 对象；格式异常时抛 :class:`ApiError`（由调用方降级）。"""
    text = await runner.complete(
        [{"role": "system", "content": system}, {"role": "user", "content": instruction}],
        temperature=temperature,
    )
    if not text or len(text) > _MAX_ROLE_OUTPUT:
        raise ApiError(422, "LLM_FORMAT_ERROR", "角色输出为空或超长，无法解析为结构化结果")
    parsed = extract_json_object(text)
    if parsed is None:
        raise ApiError(422, "LLM_FORMAT_ERROR", "角色输出不是合法 JSON 结构")
    return parsed


def _str_list(value: Any, limit: int = 20) -> list[str]:
    """把任意值收敛为「非空字符串列表」（模型偶尔会返回字符串而非数组）。"""
    if isinstance(value, str):
        return [value.strip()][:limit] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()][:limit]
    return []


def _resolve_task(tasks: list[dict[str, Any]], ref: str) -> dict[str, Any] | None:
    """任务引用 → 任务（与 What-if 沙盘共用同一解析口径，避免两处行为不一致）。"""
    from .whatif import _resolve_task as resolve

    return resolve(tasks, ref)


def _fallback_planner(requirement: str) -> dict[str, Any]:
    """planner 降级产出：按标点切分需求，把可识别的句子当作验收点（不编造内容）。"""
    chunks = [chunk.strip() for chunk in requirement.replace("；", "。").split("。") if chunk.strip()]
    return {"brief": requirement, "acceptance": chunks[:6], "assumptions": [], "risks": []}


def _fallback_critic(plan: dict[str, Any], acceptance: list[str]) -> dict[str, Any]:
    """critic 降级产出：只给**确定性核对**结果（验收点覆盖 / 关键路径责任人 / 交付日期）。"""
    gaps = acceptance_gaps(plan, acceptance)
    stats = progress_stats(plan)
    unassigned = stats.get("unassignedCriticalTasks") or []
    if unassigned:
        gaps.append(f"关键路径上有 {len(unassigned)} 个任务未指派责任人：{'、'.join(unassigned[:5])}")
    if stats.get("deadlineMet") is False:
        gaps.append(f"引擎推算完成日期 {stats.get('plannedEndDate')} 晚于截止日期 {plan.get('iterationDeadline')}")
    return {
        "ready": not gaps,
        "gaps": gaps,
        "recommendations": ["先补齐上述缺口，再进入迭代执行阶段"]
        if gaps
        else ["计划覆盖了验收点且关键路径均有责任人，可按当前排期推进"],
        "verdict": "存在需人工确认的缺口" if gaps else "计划可交付",
    }


def _pending_from_changes(plan_id: str, plan: dict[str, Any], changes: Any) -> list[PendingAction]:
    """把 estimator 的修正建议转成**待确认动作**（不落库；字段非法 / 任务不存在直接丢弃）。"""
    if not isinstance(changes, list):
        return []
    tasks = [task for task in (plan.get("taskList") or []) if isinstance(task, dict)]
    pending: list[PendingAction] = []
    for item in changes[:_MAX_PENDING]:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "").strip()
        if field not in UPDATABLE_FIELDS:
            continue
        target = _resolve_task(tasks, str(item.get("task") or ""))
        if target is None:
            continue
        value = item.get("value")
        if field == "estimatedHours":
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            if value <= 0:
                continue
        elif field in ("status", "priority"):
            value = str(value).strip().upper()
        elif field == "predecessorTaskIds":
            value = [str(entry) for entry in (value if isinstance(value, list) else [value]) if str(entry).strip()]
        else:
            value = str(value).strip()
        if value in (None, "", []):
            continue
        args = {
            "planId": plan_id,
            "taskId": str(target.get("taskId")),
            "updateField": field,
            "newValue": value,
        }
        pending.append(
            PendingAction(
                actionId=f"act_{uuid.uuid4().hex[:12]}",
                tool="task_update",
                label=describe_action("task_update", args, task_name=str(target.get("taskName") or "")),
                arguments=args,
                planId=plan_id,
            )
        )
    return pending


def _dedupe(actions: list[PendingAction]) -> list[PendingAction]:
    """同一任务同一字段只保留最后一条（模型偶尔会给出重复建议）。"""
    seen: dict[tuple[str, str], PendingAction] = {}
    for action in actions:
        seen[(str(action.arguments.get("taskId")), str(action.arguments.get("updateField")))] = action
    return list(seen.values())


def _plan_report(plan: dict[str, Any], plan_id: str, critic: dict[str, Any] | None, pending: list[PendingAction]) -> str:
    """确定性生成团队可读的最终报告（不依赖模型，保证降级路径也有完整结论）。"""
    stats = progress_stats(plan)
    names = task_map(plan)
    critical = [item for item in critical_path(plan) if item in names]
    risks = [risk for risk in (plan.get("riskList") or []) if isinstance(risk, dict)]
    lines = [
        f"已完成多角色协作规划（planId：{plan_id}）。",
        f"任务 {stats.get('taskCount')} 个，总工期 {plan.get('totalDurationHours')} 小时，"
        f"预估总工时 {plan.get('totalEstimatedHours')} 小时。",
        "关键路径（{} 个任务）：{}".format(
            len(critical), " → ".join(str(names[item].get("taskName") or item) for item in critical)
        ),
        f"风险 {len(risks)} 条，其中高风险 {sum(1 for risk in risks if str(risk.get('level')).upper() == 'HIGH')} 条。",
    ]
    if stats.get("deadlineMet") is False:
        lines.append(f"注意：引擎推算完成日期 {stats.get('plannedEndDate')} 晚于截止日期 {plan.get('iterationDeadline')}。")
    if critic:
        verdict = str(critic.get("verdict") or "")
        if verdict:
            lines.append(f"质量校验：{verdict}")
        for gap in _str_list(critic.get("gaps"), 5):
            lines.append(f"- 缺口：{gap}")
        for action in _str_list(critic.get("recommendations"), 5):
            lines.append(f"- 建议：{action}")
    if pending:
        lines.append(f"已登记 {len(pending)} 条估算修正建议待你确认（确认后由算法引擎重算），请核对确认卡片。")
    return "\n".join(lines)


async def run_collaboration(
    engine: EngineClient,
    runner: AgentRunner,
    dispatcher: ToolDispatcher,
    *,
    requirement: str,
    iteration_deadline: str | None = None,
    iteration_start_date: str | None = None,
    dev_member_count: int | None = None,
    max_steps: int = 6,
) -> AsyncIterator[dict[str, Any]]:
    """执行一次多角色协作编排，逐步产出 SSE 事件。

    事件类型（与既有聊天流一致，前端无需新协议）：

    - ``step``：角色步骤状态（running / done / failed / skipped），构成可见时间线；
    - ``tool_start`` / ``tool_result``：引擎工具调用（scheduler 的排期计算）；
    - ``pending``：estimator 提出的**待确认**修正动作（两阶段写操作第一阶段）；
    - ``final``：最终结论（``AgentRunResult``：报告 + 工具记录 + 待确认动作）。

    中断语义：路由在客户端断开时会 ``aclose`` 本生成器，**取消只发生在 await 边界**
    （即角色步骤之间）；已创建的计划保留在引擎持久化存储中（属用户真实诉求，不做回滚）。
    """
    clean_requirement = sanitize_user_input(requirement)
    if not clean_requirement:
        raise ApiError(400, "VALIDATION_ERROR", "需求不能为空或仅包含空白字符")

    deadline = iteration_deadline or (date.today() + timedelta(days=14)).isoformat()
    start = iteration_start_date or date.today().isoformat()
    members = dev_member_count if dev_member_count is not None else 3
    steps = 0
    records: list[ToolCallRecord] = []
    pending: list[PendingAction] = []
    plan_id: str | None = None
    plan: dict[str, Any] = {}
    brief = ""
    acceptance: list[str] = []

    # ------------------------------- 1) planner -------------------------------
    yield {"event": SSE_STEP, "data": _step(ROLE_PLANNER, STEP_RUNNING, "补全需求边界与验收标准")}
    if runner.available:
        try:
            parsed = await _role_json(
                runner,
                PLANNER_ROLE_PROMPT,
                build_planner_instruction(clean_requirement, iteration_deadline=deadline, dev_member_count=members),
            )
            brief = str(parsed.get("brief") or "").strip()
            acceptance = _str_list(parsed.get("acceptance"))
            assumptions = _str_list(parsed.get("assumptions"))
            detail = f"验收点 {len(acceptance)} 项"
            if assumptions:
                detail += f"，假设 {len(assumptions)} 项待确认"
            yield {"event": SSE_STEP, "data": _step(ROLE_PLANNER, STEP_DONE, detail)}
        except ApiError as exc:
            logger.warning("planner 角色失败（%s），使用降级产出", exc.message)
            fallback = _fallback_planner(clean_requirement)
            brief, acceptance = fallback["brief"], fallback["acceptance"]
            yield {
                "event": SSE_STEP,
                "data": _step(ROLE_PLANNER, STEP_SKIPPED, f"模型不可用（{exc.code}），已改用需求原文作为验收点"),
            }
    else:
        fallback = _fallback_planner(clean_requirement)
        brief, acceptance = fallback["brief"], fallback["acceptance"]
        yield {"event": SSE_STEP, "data": _step(ROLE_PLANNER, STEP_SKIPPED, "未配置 LLM，跳过需求补全（直接按需求原文拆解）")}

    # ------------------------------ 2) scheduler ------------------------------
    engine_requirement = (
        clean_requirement
        if not brief or brief == clean_requirement
        else f"{clean_requirement}\n\n【需求补充说明】\n{brief}"
    )
    yield {"event": SSE_STEP, "data": _step(ROLE_SCHEDULER, STEP_RUNNING, "调用算法引擎执行拆解 → DAG → AOE → 风险 → 燃尽")}
    yield {"event": SSE_TOOL_START, "data": {"name": "plan_iteration"}}
    call_args: dict[str, Any] = {
        "requirement": engine_requirement,
        "iterationDeadline": deadline,
        "devMemberCount": members,
        "iterationStartDate": start,
    }
    started = time.monotonic()
    try:
        result = await dispatcher.dispatch("plan_iteration", dict(call_args))
    except ApiError as exc:
        records.append(ToolCallRecord(name="plan_iteration", arguments=dict(call_args), ok=False, error=exc.message))
        yield {
            "event": SSE_TOOL_RESULT,
            "data": {"name": "plan_iteration", "arguments": dict(call_args), "ok": False, "error": exc.message},
        }
        yield {"event": SSE_STEP, "data": _step(ROLE_SCHEDULER, STEP_FAILED, "算法引擎调用失败，本轮协作终止")}
        METRICS.inc_counter("collab_runs_total", {"outcome": "engine_failed"})
        raise
    plan = result if isinstance(result, dict) else {}
    plan_id = str(plan.get("planId")) if plan.get("planId") else None
    steps += 1
    records.append(
        ToolCallRecord(
            name="plan_iteration",
            arguments=dict(call_args),
            ok=True,
            result={"planId": plan_id, "taskCount": len(plan.get("taskList") or [])},
        )
    )
    yield {
        "event": SSE_TOOL_RESULT,
        "data": {
            "name": "plan_iteration",
            "arguments": dict(call_args),
            "ok": True,
            "error": None,
            "elapsedMs": int((time.monotonic() - started) * 1000),
        },
    }
    yield {
        "event": SSE_STEP,
        "data": _step(
            ROLE_SCHEDULER,
            STEP_DONE,
            f"计划 {plan_id}：任务 {len(plan.get('taskList') or [])} 个，总工期 {plan.get('totalDurationHours')} 小时",
        ),
    }

    # ------------------------------ 3) estimator ------------------------------
    if plan_id and runner.available and steps < max_steps:
        yield {"event": SSE_STEP, "data": _step(ROLE_ESTIMATOR, STEP_RUNNING, "审阅工时与依赖合理性")}
        try:
            parsed = await _role_json(runner, ESTIMATOR_ROLE_PROMPT, build_estimator_instruction(plan, brief))
            changes = parsed.get("changes")
            count = len(changes) if isinstance(changes, list) else 0
            pending = _pending_from_changes(plan_id, plan, changes)
            steps += 1
            detail = f"提出 {count} 条修正建议，其中 {len(pending)} 条有效（已登记待确认）"
            summary = str(parsed.get("summary") or "").strip()
            if summary:
                detail += f"；{summary[:80]}"
            yield {"event": SSE_STEP, "data": _step(ROLE_ESTIMATOR, STEP_DONE, detail)}
        except ApiError as exc:
            logger.warning("estimator 角色失败（%s），跳过", exc.message)
            yield {"event": SSE_STEP, "data": _step(ROLE_ESTIMATOR, STEP_FAILED, f"角色执行失败（{exc.code}），已跳过")}
    else:
        yield {
            "event": SSE_STEP,
            "data": _step(ROLE_ESTIMATOR, STEP_SKIPPED, "未配置 LLM" if not runner.available else "已达本步骤上限"),
        }

    # -------------------------------- 4) risk --------------------------------
    if plan_id and steps < max_steps:
        yield {"event": SSE_STEP, "data": _step(ROLE_RISK, STEP_RUNNING, "为引擎检出的风险补充缓解动作")}
        try:
            advice = await generate_advice(engine, runner, model=None, plan_id=plan_id)
            steps += 1
            detail = f"风险 {len(advice.items)} 条已配缓解动作"
            if advice.degradedReason:
                detail += f"（{advice.degradedReason}）"
            yield {"event": SSE_STEP, "data": _step(ROLE_RISK, STEP_DONE, detail)}
        except ApiError as exc:
            logger.warning("risk 角色失败（%s），跳过", exc.message)
            yield {"event": SSE_STEP, "data": _step(ROLE_RISK, STEP_FAILED, f"角色执行失败（{exc.code}），已跳过")}
    else:
        yield {"event": SSE_STEP, "data": _step(ROLE_RISK, STEP_SKIPPED, "缺少计划或已达步骤上限")}

    # ------------------------------- 5) critic -------------------------------
    critic: dict[str, Any] | None = None
    if plan_id and steps < max_steps:
        yield {"event": SSE_STEP, "data": _step(ROLE_CRITIC, STEP_RUNNING, "校验需求覆盖 / 责任人 / 交付日期")}
        deterministic = _fallback_critic(plan, acceptance)
        if runner.available:
            try:
                parsed = await _role_json(runner, CRITIC_ROLE_PROMPT, build_critic_instruction(plan, acceptance, brief))
                # 确定性核对结果**并入**模型结论：模型漏掉「关键路径无人负责」这类硬事实时仍能报出
                gaps = sorted(set(_str_list(parsed.get("gaps")) + deterministic["gaps"]))
                critic = {
                    "ready": bool(parsed.get("ready")) and not deterministic["gaps"],
                    "gaps": gaps,
                    "recommendations": _str_list(parsed.get("recommendations")) or deterministic["recommendations"],
                    "verdict": str(parsed.get("verdict") or deterministic["verdict"]),
                }
                steps += 1
                yield {
                    "event": SSE_STEP,
                    "data": _step(ROLE_CRITIC, STEP_DONE, f"结论：{critic['verdict']}（缺口 {len(gaps)} 项）"),
                }
            except ApiError as exc:
                logger.warning("critic 角色失败（%s），使用确定性核对", exc.message)
                critic = deterministic
                yield {
                    "event": SSE_STEP,
                    "data": _step(ROLE_CRITIC, STEP_DONE, f"模型不可用（{exc.code}），仅执行确定性核对（缺口 {len(deterministic['gaps'])} 项）"),
                }
        else:
            critic = deterministic
            yield {
                "event": SSE_STEP,
                "data": _step(ROLE_CRITIC, STEP_DONE, f"未配置 LLM，仅执行确定性核对（缺口 {len(deterministic['gaps'])} 项）"),
            }
    else:
        yield {"event": SSE_STEP, "data": _step(ROLE_CRITIC, STEP_SKIPPED, "缺少计划或已达步骤上限")}

    # -------------------------------- final ---------------------------------
    if pending:
        pending = _dedupe(pending)
        yield {"event": SSE_PENDING, "data": {"actions": [item.model_dump() for item in pending]}}
    content = _plan_report(plan, plan_id or "（未生成）", critic, pending)
    METRICS.inc_counter("collab_runs_total", {"outcome": "completed"})
    yield {
        "event": SSE_FINAL,
        "data": AgentRunResult(
            content=content,
            planId=plan_id,
            toolCalls=records,
            steps=steps,
            pendingActions=pending,
        ).model_dump(),
    }


__all__ = [
    "run_collaboration",
    "ROLE_PLANNER",
    "ROLE_SCHEDULER",
    "ROLE_ESTIMATOR",
    "ROLE_RISK",
    "ROLE_CRITIC",
    "ROLE_LABELS",
    "STEP_RUNNING",
    "STEP_DONE",
    "STEP_FAILED",
    "STEP_SKIPPED",
]

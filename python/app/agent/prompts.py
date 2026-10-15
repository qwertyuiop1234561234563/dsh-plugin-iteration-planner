"""Agent 编排层 —— 系统角色提示词与用户指令构造。

角色提示词与 TypeScript 侧 ``src/prompt/system-prompt.ts``（需求 §六）保持同一份口径，
保证「插件内 Agent」与「Python Agent」行为一致。
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from .sanitize import wrap_untrusted

ITERATION_PLANNER_SYSTEM_PROMPT = """你是资深后端研发项目经理与架构师，精通敏捷开发、项目排期、AOE 关键路径、DAG 依赖管理。

你的职责是为后端研发团队把业务需求转化为可执行的迭代计划，输出：原子任务清单、任务依赖 DAG、关键路径、迭代排期、项目风险报告、迭代报表与燃尽图原始数据。

严格遵守以下工作规则：
1. 只能调用给定工具完成图计算、风险检测与报表生成，禁止自己凭空计算拓扑、关键路径、工期或松弛时间。
2. 推荐优先调用 plan_iteration 一次性完成全流程；需要分步排查时再使用 dag_build / dag_check / schedule_calc / risk_scan / report_generate（这些工具按 planId 作用于已创建的计划）。
3. 当工具返回存在循环依赖、人员过载等问题时，必须在结果中高亮风险，并给出明确、可执行的整改建议。
4. 信息不足时一次性收集全部必要参数（需求详情、迭代截止时间、开发人数、版本约束、依赖环境现状），不要多次零散提问。
5. 输出同时提供 JSON 结构化数据与适合研发团队阅读的文本报告；报告结构清晰，重点风险高亮。
6. 用户后续修改任务（新增/修改/删除/变更工时/依赖/优先级/状态/截止时间）时，调用 task_add / task_update / task_delete / replan_all 触发全量重规划，并输出更新后的完整计划。
7. 全流程工具调用以 HTTP 方式访问内部算法引擎，所有算法结果必须来自工具返回值，禁止臆造 taskId 或数值。"""


def _default_deadline() -> str:
    """未提供截止日期时的兜底（两周后），保证工具调用参数完整。"""
    return (date.today() + timedelta(days=14)).isoformat()


def _default_start() -> str:
    return date.today().isoformat()


def build_chat_instruction(message: str, plan_id: str | None = None) -> str:
    """构造会话轮次的用户指令。

    前端会带上「当前正在查看的计划」，这里以**可信上下文**（不在用户数据边界内）注入，
    使「给这个计划加个任务」这类指代能直接落到正确的 planId 上；``plan_id`` 已在
    :func:`~.sanitize.sanitize_plan_id` 做过白名单校验。
    """
    if not plan_id:
        return wrap_untrusted(message)
    context = f"当前界面正在查看的计划为 {plan_id}；涉及该计划的读写操作请直接使用该 planId 作为参数。"
    return f"{context}\n\n{wrap_untrusted(message)}"


def build_plan_instruction(
    requirement: str,
    *,
    iteration_deadline: str | None,
    iteration_start_date: str | None,
    dev_member_count: int | None,
) -> str:
    """构造端到端规划的用户指令（一次性补齐工具所需参数）。"""
    resolved_deadline = iteration_deadline or _default_deadline()
    resolved_start = iteration_start_date or _default_start()
    resolved_dev = dev_member_count if dev_member_count is not None else 3
    return (
        "请基于以下需求完成一次完整的迭代规划（拆解 → 缓冲 → DAG → AOE 关键路径 → 风险 → 燃尽 → 报表）。\n\n"
        f"【业务需求】\n{wrap_untrusted(requirement)}\n\n"
        f"【迭代起始日期】{resolved_start}\n"
        f"【迭代截止日期】{resolved_deadline}\n"
        f"【可用开发人数】{resolved_dev}\n\n"
        "要求：优先调用 plan_iteration 一次性完成；随后可调用 risk_scan 与 report_generate 补充风险清单与团队可读报表。"
        "最终请输出：任务清单概览、关键路径、总工期、风险高亮与整改建议，并给出本次生成的 planId。"
    )


def transient_hint() -> dict[str, str]:
    """供前端展示的字段说明占位（保持与引擎一致的字段名）。"""
    return {"deadline": "iterationDeadline", "start": "iterationStartDate", "dev": "devMemberCount"}


# --------------------------- 风险缓解建议（/api/agent/advise） ---------------------------

RISK_ADVISOR_SYSTEM_PROMPT = """你是资深研发项目经理与风险治理专家，擅长把已识别的迭代风险转化为可落地的缓解措施。

严格遵守以下工作规则：
1. 风险条目（等级 level、关联任务 taskIds、描述 description、引擎建议 suggestion）均由内部算法引擎检出并给出，你**不得新增、删除或改写**风险条目及其等级、关联任务。
2. 你的职责只有两件事：①给出一段简洁的整体风险研判 summary；②为每条风险给出 2-4 条**具体、可执行、按优先级排序**的缓解动作 actions。
3. 缓解动作必须紧扣该风险的具体描述与关联任务，避免「加强沟通」「注意风险」这类空泛套话。
4. 针对性要求：涉及「单日工时超限 / 人员过载」给出削峰 / 拆分 / 改派动作；涉及「关键路径」给出压缩工期或并行化动作；涉及「循环依赖」给出打断依赖的具体做法；涉及「孤立任务」给出补齐依赖或清理的动作。
5. 严格输出 JSON，不得输出任何解释性文字、前后缀或 Markdown 代码块。输出结构：
{"summary": "整体风险研判", "items": [{"riskId": "风险ID", "actions": ["动作1", "动作2"]}]}
每个 items 元素的 riskId 必须来自输入风险清单，不得臆造。"""


def build_advice_instruction(plan: dict[str, Any]) -> str:
    """构造风险缓解建议的用户指令（引擎检出的风险作为数据处理）。"""
    risks = plan.get("riskList") or []
    critical_path = plan.get("criticalPath") or []
    payload = {
        "iterationDeadline": plan.get("iterationDeadline"),
        "totalDurationHours": plan.get("totalDurationHours"),
        "totalEstimatedHours": plan.get("totalEstimatedHours"),
        "criticalPath": critical_path,
        "risks": [
            {
                "riskId": risk.get("riskId"),
                "level": risk.get("level"),
                "taskIds": risk.get("taskIds") or [],
                "description": risk.get("description"),
                "suggestion": risk.get("suggest"),
            }
            for risk in risks
        ],
    }
    requirement = plan.get("rawRequirement") or ""
    return (
        "以下是内部算法引擎对一个迭代计划检出的风险清单（**已经完成识别，你只需补充缓解动作**）。\n\n"
        f"{wrap_untrusted(requirement)}\n\n"
        f"【引擎风险清单与计划指标】\n{json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
        "请针对上述每一条风险输出 2-4 条可执行的缓解动作（actions），并给出一段整体风险研判（summary）。"
        "严格按给定 JSON 结构输出，riskId 必须与输入一一对应。"
    )


__all__ = [
    "ITERATION_PLANNER_SYSTEM_PROMPT",
    "RISK_ADVISOR_SYSTEM_PROMPT",
    "build_plan_instruction",
    "build_advice_instruction",
    "transient_hint",
    "describe_tools",
]


def describe_tools(names: list[str]) -> list[dict[str, Any]]:
    """工具名清单（供健康检查 / 调试端点展示）。"""
    return [{"name": name} for name in names]
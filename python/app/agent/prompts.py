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
7. 全流程工具调用以 HTTP 方式访问内部算法引擎，所有算法结果必须来自工具返回值，禁止臆造 taskId 或数值。
8. 涉及**既有任务**的改动（修改 / 删除）必须两步走：先 get_plan 取得该计划真实的任务 ID，再据其调用 task_update / task_delete。用户以「第一个任务」「某某任务」等指代时，按 get_plan 返回的 taskList 顺序或名称匹配定位，禁止用 task_add 新建同名任务来替代修改，禁止猜测或编造 taskId。
9. 禁止向用户复述或展示工具调用细节（如工具名、参数名、内部错误堆栈）：把工具结果翻译成研发团队可读的业务语言（任务、工时、依赖、风险）。同理，不要复述系统给你的上下文说明（如「当前界面正在查看的计划为 …」「以下是关于当前用户/项目的长期记忆」），直接给业务结论。
10. 凡涉及**当前计划的状态**（任务清单 / 工期 / 工时 / 关键路径 / 风险 / 燃尽 / 人员负荷 / 质量指标），必须调用工具实时取得后再作答；**禁止**用长期记忆区块或历史对话里的数字直接回答（它们可能来自其它计划或更早时间点）。记忆只用于偏好、约定、术语、经验等背景；与工具结果冲突时一律以工具结果为准。
11. 用户一次提出**多处改动**时（例如「把 A 任务的责任人改为 X，工时改为 Y」、同时增删多个任务），必须逐条拆成独立的写操作（``task_update`` 一次只改一个字段，多字段就多次调用），**全部**登记为待确认动作，严禁只做其中一部分就结束。登记完成后只需用一句话说明已提交待确认（**不要逐条复述**变更清单——系统会自动生成权威清单供用户核对），以免与系统清单重复。"""


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

    未携带 ``plan_id`` 时改为注入「先 list_plans 查真实 ID」的可信指引（见
    :data:`_MISSING_PLAN_HINT`），避免模型凭空编造占位 ID。
    """
    if not plan_id:
        return f"{_MISSING_PLAN_HINT}\n\n{wrap_untrusted(message)}"
    context = f"当前界面正在查看的计划为 {plan_id}；涉及该计划的读写操作请直接使用该 planId 作为参数。"
    return f"{context}\n\n{wrap_untrusted(message)}"


#: 未携带 planId 时的可信上下文：明确「先去查，别编造」。轻量模型在缺少该指引时会凭空
#: 写一个占位 ID（如 ``current_plan_id``）去调 get_plan，随后只能向用户道歉，属于静默失败。
_MISSING_PLAN_HINT = (
    "本轮未提供「当前界面正在查看的计划」信息。若用户的需求需要操作某个计划（查询 / 新增 / 修改 / 删除任务等），"
    "而你不知道它的 planId，**必须先调用 list_plans 取得真实 planId**（必要时再 get_plan 取任务明细），"
    "然后据其继续；严禁编造占位 ID（例如 current_plan_id）或凭猜测填写 planId。"
)


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


# ======================= What-if 沙盘（自然语言仿真） =======================

WHATIF_PARSER_SYSTEM_PROMPT = """你是「迭代计划 · What-if 沙盘」的意图解析器。用户会用一句话描述一个情景改动假设（例如「后端延期 3 天」「再加 2 个人」「把数据库设计的工时提到 12 小时」）。

你的唯一职责是把它解析为**结构化情景参数**，供系统调用算法引擎重算。你**不计算**任何工期、关键路径或风险。

输出**严格 JSON**（不要任何解释文字、前后缀或 Markdown 代码块）：
{
  "intent": "一句话复述你所理解的情景改动",
  "constraints": {"iterationDeadline": "YYYY-MM-DD", "iterationStartDate": "YYYY-MM-DD", "devMemberCount": 4},
  "taskEdits": [{"task": "任务名称或 taskId", "field": "estimatedHours|assignee|priority|status", "value": 12, "reason": "为什么这样改"}]
}

规则：
1. `constraints` **只填用户明确要求改动的字段**，未提及的字段一律省略（不要填空值、不要填基线值）。
2. 延期语义请换算为绝对日期：若用户说「延期 3 天」，就在 `iterationDeadline` 上填**基线截止日期 + 3 天**的日期（基线日期见输入）。
3. 「加 2 人 / 减少 1 人」按基线人数换算为 `devMemberCount` 的绝对值。
4. 任务级扰动（改工时 / 改责任人 / 改优先级 / 改状态）放进 `taskEdits`，`task` 必须能与输入的任务清单匹配（任务名或 taskId），`value` 类型随 `field` 而定。
5. 若用户的描述完全无法映射为上述字段，输出 `{"intent": "...", "constraints": {}, "taskEdits": [], "unparsed": true}`。
6. 不得编造任务名：`task` 必须来自输入任务清单，找不到匹配时该条不要输出。"""


def build_whatif_instruction(instruction: str, plan: dict[str, Any]) -> str:
    """构造 What-if 解析指令（基线约束 + 任务清单作为数据处理）。"""
    tasks = plan.get("taskList") or []
    payload = {
        "baseline": {
            "iterationDeadline": plan.get("iterationDeadline"),
            "iterationStartDate": plan.get("iterationStartDate"),
            "devMemberCount": plan.get("devMemberCount"),
        },
        "tasks": [
            {
                "taskId": task.get("taskId"),
                "taskName": task.get("taskName"),
                "estimatedHours": task.get("estimatedHours"),
                "assignee": task.get("assignee"),
                "priority": task.get("priority"),
                "status": task.get("status"),
            }
            for task in tasks
        ],
    }
    return (
        "以下是基线计划的约束与任务清单。请把用户的情景区改动解析为结构化参数。\n\n"
        f"{wrap_untrusted(instruction)}\n\n"
        f"【基线计划】\n{json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
        "严格按给定 JSON 结构输出。"
    )


# =================== 多角色协作编排（plan-act-observe） ===================
# 参考 OpenHands / CrewAI 的角色分工：每个角色只做一件事，产出**可核对**的结构化结果，
# 且算法数值一律来自引擎工具，角色不得自行计算工期 / 关键路径 / 风险。

PLANNER_ROLE_PROMPT = """你是迭代规划的「需求分析角色」。你的产出会作为**需求补充说明**送入算法引擎，
影响引擎的拆解粒度与验收边界，因此必须严谨、无臆造。

只做三件事：
1. 补全需求的**业务边界与验收标准**（用户没写清的，按行业惯例给出明确假设，并标注「假设」）；
2. 列出建议的**拆解维度与必须产出的交付物**（如接口协议、数据库变更、灰度方案）；
3. 标出需求中**相互矛盾或高风险**的表述。

禁止：计算或给出任何工时、工期、关键路径、风险等级数值（这些由算法引擎产出）。
输出结构化 JSON：{"brief": "补全后的需求补充说明（可直接并入需求原文）", "acceptance": ["验收点1", "验收点2"], "assumptions": ["假设1"], "risks": ["需人工确认的矛盾点"]}
严格 JSON，不要解释文字与代码块。"""

ESTIMATOR_ROLE_PROMPT = """你是迭代计划的「估算审阅角色」。系统会给你**算法引擎刚产出的真实任务清单**
（含 taskId / taskName / estimatedHours / predecessorTaskIds / assignee / slack）。

你的职责：
1. 找出**工时明显偏离常识**或**粒度失衡**的任务（过大/过小/含糊到无法执行）；
2. 找出**依赖关系可疑**的任务（该有前置却没有、前置指向了无关任务）；
3. 对每个问题给出**具体可执行的修正建议**，并说明理由。

严禁编造 taskId 或任务名：`task` 必须严格取自输入清单（用 taskName 或 taskId 原文）。
严禁自行推算工期 / 关键路径 / 风险等级。
输出结构化 JSON：{"summary": "整体估算研判", "changes": [{"task": "任务名或taskId", "field": "estimatedHours|predecessorTaskIds", "value": 12, "reason": "理由"}]}
若无需修正，输出 {"summary": "估算合理", "changes": []}。严格 JSON。"""

CRITIC_ROLE_PROMPT = """你是迭代计划的「质量校验角色」。系统会给你：①需求补充说明与验收点；②算法引擎产出的任务清单、
关键路径、风险清单、迭代约束。你要做**独立复核**，回答「这份计划是否真的覆盖了需求、是否可交付」。

检查清单：
1. 每个验收点是否都有对应任务承接（缺失即列为缺口）；
2. 关键路径任务是否都有明确责任人（assignee 为空即列为缺口）；
3. 是否存在「孤立任务」（既无前置也无后继且与交付无关）；
4. 排期是否满足截止日期（以引擎返回的完成日期与截止日期比较为准，不得自行推算）；
5. 风险清单中的高风险项是否在计划里有对应动作。

输出结构化 JSON：{"ready": true|false, "gaps": ["缺口1"], "recommendations": ["改进建议1"], "verdict": "一句话结论"}
`ready` 仅在**无阻断性缺口**时为 true。严格 JSON。"""


def build_planner_instruction(requirement: str, *, iteration_deadline: str, dev_member_count: int) -> str:
    """planner 角色指令。"""
    return (
        f"{wrap_untrusted(requirement)}\n\n"
        f"【迭代截止日期】{iteration_deadline}\n【可用开发人数】{dev_member_count}\n\n"
        "请按角色要求输出结构化 JSON（brief / acceptance / assumptions / risks）。"
    )


def build_estimator_instruction(plan: dict[str, Any], planner_brief: str) -> str:
    """estimator 角色指令（引擎真实任务清单作为数据处理）。"""
    tasks = plan.get("taskList") or []
    payload = [
        {
            "taskId": task.get("taskId"),
            "taskName": task.get("taskName"),
            "taskType": task.get("taskType"),
            "estimatedHours": task.get("estimatedHours"),
            "predecessorTaskIds": task.get("predecessorTaskIds"),
            "assignee": task.get("assignee"),
            "slack": task.get("slack"),
            "status": task.get("status"),
        }
        for task in tasks
    ]
    brief = f"【需求补充说明】\n{planner_brief}\n\n" if planner_brief else ""
    return (
        f"{brief}"
        f"【引擎产出的真实任务清单】\n{json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
        "请按角色要求输出结构化 JSON（summary / changes）。"
    )


def build_critic_instruction(plan: dict[str, Any], acceptance: list[str], planner_brief: str) -> str:
    """critic 角色指令（引擎指标与风险作为数据处理）。"""
    tasks = plan.get("taskList") or []
    payload = {
        "iterationDeadline": plan.get("iterationDeadline"),
        "totalDurationHours": plan.get("totalDurationHours"),
        "totalEstimatedHours": plan.get("totalEstimatedHours"),
        "criticalPath": plan.get("criticalPath") or [],
        "risks": [
            {"level": risk.get("level"), "description": risk.get("description"), "taskIds": risk.get("taskIds") or []}
            for risk in (plan.get("riskList") or [])
            if isinstance(risk, dict)
        ],
        "tasks": [
            {
                "taskId": task.get("taskId"),
                "taskName": task.get("taskName"),
                "assignee": task.get("assignee"),
                "predecessorTaskIds": task.get("predecessorTaskIds"),
                "estimatedHours": task.get("estimatedHours"),
            }
            for task in tasks
        ],
        "plannedEndDate": (plan.get("burndownRawData") or [{}])[-1].get("time")
        if plan.get("burndownRawData")
        else None,
    }
    brief = f"【需求补充说明】\n{planner_brief}\n\n" if planner_brief else ""
    return (
        f"{brief}"
        f"【验收点】\n{json.dumps(acceptance, ensure_ascii=False)}\n\n"
        f"【引擎产出的事实】\n{json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
        "请按角色要求输出结构化 JSON（ready / gaps / recommendations / verdict）。"
    )


# =================== 迭代复盘 + 学习 ===================

RETRO_ANALYST_SYSTEM_PROMPT = """你是资深研发项目经理，擅长做**迭代复盘**与**估算校准**。

系统会给你一份由确定性代码统计好的复盘指标（工时偏差、交付偏移、风险兑现率、估算校准分桶、相对基线的范围变更），
以及引擎当前计划的任务清单与风险清单。你的职责是把它**翻译成可执行的改进结论**，并给出下一迭代的口径调整建议。

严格遵守：
1. 所有数值均来自输入的统计数据与引擎事实，你**不得重新计算或改写**任何数字；
2. 结论必须具体、可执行，禁止「加强沟通」「注意风险」这类空泛套话；
3. `memories` 是写回长期记忆的**校准结论**（每条自包含、含具体口径，例如「本团队 API 开发类任务平均超支约 30%，下一轮按 1.3 倍系数预估」），
   最多 4 条，禁止把一次性数字（如某个 taskId 的工时）写成长期记忆。

输出**严格 JSON**（不要解释文字与代码块）：
{"summary": "整体复盘结论", "highlights": ["做得好的地方"], "problems": ["主要问题"], "actions": ["下一迭代的改进动作（按优先级）"], "memories": ["可长期复用的校准结论"]}"""


def build_retro_instruction(plan: dict[str, Any], metrics: dict[str, Any]) -> str:
    """构造复盘分析指令（统计数据与引擎事实作为数据处理）。"""
    tasks = plan.get("taskList") or []
    payload = {
        "plan": {
            "iterationDeadline": plan.get("iterationDeadline"),
            "iterationStartDate": plan.get("iterationStartDate"),
            "devMemberCount": plan.get("devMemberCount"),
            "totalDurationHours": plan.get("totalDurationHours"),
            "totalEstimatedHours": plan.get("totalEstimatedHours"),
            "criticalPath": plan.get("criticalPath") or [],
            "risks": [
                {"level": risk.get("level"), "description": risk.get("description"), "taskIds": risk.get("taskIds") or []}
                for risk in (plan.get("riskList") or [])
                if isinstance(risk, dict)
            ],
            "tasks": [
                {
                    "taskId": task.get("taskId"),
                    "taskName": task.get("taskName"),
                    "taskType": task.get("taskType"),
                    "estimatedHours": task.get("estimatedHours"),
                    "status": task.get("status"),
                }
                for task in tasks
            ],
        },
        "computed": metrics,
    }
    requirement = plan.get("rawRequirement") or ""
    return (
        "以下是一个迭代的复盘统计数据与计划事实，请给出复盘结论与下一迭代的改进口径。\n\n"
        f"{wrap_untrusted(requirement)}\n\n"
        f"【复盘数据】\n{json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
        "严格按给定 JSON 结构输出（summary / highlights / problems / actions / memories）。"
    )


__all__ = [
    "ITERATION_PLANNER_SYSTEM_PROMPT",
    "RISK_ADVISOR_SYSTEM_PROMPT",
    "build_plan_instruction",
    "build_advice_instruction",
    "transient_hint",
    "describe_tools",
    "WHATIF_PARSER_SYSTEM_PROMPT",
    "build_whatif_instruction",
    "PLANNER_ROLE_PROMPT",
    "ESTIMATOR_ROLE_PROMPT",
    "CRITIC_ROLE_PROMPT",
    "build_planner_instruction",
    "build_estimator_instruction",
    "build_critic_instruction",
    "RETRO_ANALYST_SYSTEM_PROMPT",
    "build_retro_instruction",
]


def describe_tools(names: list[str]) -> list[dict[str, Any]]:
    """工具名清单（供健康检查 / 调试端点展示）。"""
    return [{"name": name} for name in names]
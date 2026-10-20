"""Agent 工具 schema（OpenAI function calling 格式）。

工具名与参数与需求 §四 的工具定义保持一致；每个工具由 :class:`ToolDispatcher`
映射为一次对内部算法引擎的 REST 调用，Python 侧不实现任何算法。
"""

from __future__ import annotations

from typing import Any

TASK_TYPE_VALUES = ["DB_DESIGN", "API_DEV", "BUSINESS_CODE", "DEBUG", "TEST", "DEPLOY", "DOC"]
TASK_STATUS_VALUES = ["TODO", "IN_PROGRESS", "BLOCKED", "TESTING", "DONE", "DELAYED"]
PRIORITY_VALUES = ["P0", "P1", "P2", "P3"]
UPDATABLE_FIELDS = ["estimatedHours", "predecessorTaskIds", "status", "priority", "assignee"]


def _fn(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or [],
                "additionalProperties": False,
            },
        },
    }


_ISO = {"type": "string", "description": "日期，格式 YYYY-MM-DD"}

TOOL_SPECS: list[dict[str, Any]] = [
    _fn(
        "plan_iteration",
        "编排入口：一次调用跑完 拆解→缓冲→DAG→AOE→风险→燃尽→报表，并持久化计划。返回完整 ProjectPlan（含 planId）。",
        {
            "requirement": {"type": "string", "description": "业务需求原文"},
            "iterationDeadline": _ISO,
            "iterationStartDate": _ISO,
            "devMemberCount": {"type": "integer", "minimum": 1, "description": "可用开发人数"},
        },
        ["requirement", "iterationDeadline", "devMemberCount"],
    ),
    _fn(
        "replan_all",
        "对既有计划触发全量重规划，可覆盖迭代截止日期 / 起始日期 / 开发人数。",
        {
            "planId": {"type": "string"},
            "iterationDeadline": _ISO,
            "iterationStartDate": _ISO,
            "devMemberCount": {"type": "integer", "minimum": 1},
        },
        ["planId"],
    ),
    _fn(
        "get_plan",
        "读取计划完整快照（含 taskList：每个任务的 taskId / taskName / estimatedHours / "
        "predecessorTaskIds / assignee 等）。**凡是要改动既有任务的场景，都必须先调用本工具取得真实 taskId**："
        "用户用「第一个任务 / 某名称」指代时，按 taskList 顺序（即 DAG 拓扑序）定位对应 taskId，"
        "严禁凭位置、名称或猜测编造 taskId。",
        {"planId": {"type": "string"}},
        ["planId"],
    ),
    _fn("list_plans", "列出全部计划摘要（用于确认 planId，不返回任务明细）。", {}),
    _fn(
        "task_add",
        "**新增**一个任务并自动全量重规划。仅当用户明确要求「新增 / 增加 / 补充」任务时使用；"
        "若用户是要修改或删除**既有**任务（如「把第一个任务的工时改成 12」），必须改用 task_update / "
        "task_delete，并先 get_plan 取得真实 taskId——不要用本工具新建一个同名任务来「代替修改」。",
        {
            "planId": {"type": "string"},
            "taskName": {"type": "string"},
            "taskDesc": {"type": "string"},
            "taskType": {"type": "string", "enum": TASK_TYPE_VALUES},
            "estimatedHours": {"type": "number", "exclusiveMinimum": 0},
            "predecessorTaskIds": {
                "type": "array",
                "items": {"type": "string"},
                "description": "前置任务 ID 列表，必须是 get_plan 返回的真实 taskId",
            },
            "status": {"type": "string", "enum": TASK_STATUS_VALUES},
            "priority": {"type": "string", "enum": PRIORITY_VALUES},
            "assignee": {"type": "string"},
        },
        ["planId", "taskName"],
    ),
    _fn(
        "task_update",
        "修改**既有**任务的单个字段并自动全量重规划。taskId 必须是 get_plan 返回的真实值；"
        "调用前若不确定 taskId，先 get_plan（用户说的「第 N 个任务」对应 taskList 中的第 N 项）。"
        "前置依赖字段名是 predecessorTaskIds（不是 dependencies）。",
        {
            "planId": {"type": "string"},
            "taskId": {"type": "string"},
            "updateField": {"type": "string", "enum": UPDATABLE_FIELDS},
            "newValue": {"description": "新值；类型随 updateField 而定（枚举字段见 updateField 定义）"},
        },
        ["planId", "taskId", "updateField", "newValue"],
    ),
    _fn(
        "task_delete",
        "删除**既有**任务并清理其它任务的依赖引用（自动重规划）。taskId 必须是 get_plan 返回的真实值。",
        {"planId": {"type": "string"}, "taskId": {"type": "string"}},
        ["planId", "taskId"],
    ),
    _fn("dag_build", "构建并读取 DAG 视图（节点 / 边 / 关键路径 / 环 / 孤立任务）。", {"planId": {"type": "string"}}, ["planId"]),
    _fn("dag_check", "执行 Kahn 拓扑校验，返回是否成环与环内任务。", {"planId": {"type": "string"}}, ["planId"]),
    _fn("schedule_calc", "读取关键路径与总工期（AOE 计算结果）。", {"planId": {"type": "string"}}, ["planId"]),
    _fn("risk_scan", "读取风险清单（循环依赖 / 人员过载 / 关键路径延期 / 孤立任务 / 语义风险）。", {"planId": {"type": "string"}}, ["planId"]),
    _fn("report_generate", "生成迭代报表（Markdown 文本 + 燃尽图原始数据）。", {"planId": {"type": "string"}}, ["planId"]),
    # ---------------- 知识库 / 长期记忆（agentic RAG：模型按需自行检索，只读） ----------------
    _fn(
        "kb_search",
        "检索企业知识库（历史迭代文档、规范、会议纪要等）。当用户询问与排期无关的规范 / 背景 / 历史决策，"
        "或需要引用既有文档作为依据时调用。返回命中片段与其来源文档。",
        {
            "query": {"type": "string", "description": "检索关键词或问题（自然语言即可）"},
            "topK": {"type": "integer", "minimum": 1, "maximum": 20, "description": "返回片段数，缺省 4"},
        },
        ["query"],
    ),
    _fn("kb_list_documents", "列出知识库中已收录的文档清单（用于确认有哪些资料可用）。", {}),
    _fn(
        "memory_search",
        "按语义召回关于当前用户 / 项目的长期记忆（偏好、约定、已拍板决策、硬性约束等）。"
        "当需要确认用户此前的要求或习惯时调用。",
        {"query": {"type": "string", "description": "要回忆的主题（自然语言）"}},
        ["query"],
    ),
]

TOOL_NAMES: list[str] = [spec["function"]["name"] for spec in TOOL_SPECS]

# ------------------------------- 写操作两阶段确认 -------------------------------
#
# 「写类工具」会改变计划状态（新建计划 / 重规划 / 增删改任务），因此不允许由模型直接落库：
# 编排循环只把它们登记为“待确认动作”返回前端，用户确认后才真正执行（详见 ``AgentRunner``）。

#: 写类工具名（会改变计划状态，需用户确认）
WRITE_TOOL_NAMES: tuple[str, ...] = ("plan_iteration", "replan_all", "task_add", "task_update", "task_delete")
WRITE_TOOLS: frozenset[str] = frozenset(WRITE_TOOL_NAMES)

#: 检索类工具名（直连本进程知识库 / 长期记忆，不经上游引擎）。集中登记的唯一目的：
#: 让「未启用向量化能力」的运行时（如 MCP stdio 子进程、无向量库的测试环境）能**准确裁剪**
#: 工具清单，而不是向调用方声明一个必定报「未启用」的工具。
RETRIEVAL_TOOL_NAMES: tuple[str, ...] = ("kb_search", "kb_list_documents", "memory_search")
RETRIEVAL_TOOLS: frozenset[str] = frozenset(RETRIEVAL_TOOL_NAMES)

#: 写工具「仅登记、尚未落库」时回填的记录状态：用于区分**真正落库**与**待确认**——
#: 两者都返回 ``ok=True``（调用本身都被成功受理），因此不能只凭 ``ok`` 判断数据是否已写入。
TOOL_RESULT_PENDING_CONFIRMATION = "PENDING_CONFIRMATION"

#: 工具名 → 中文动作名（确认卡片与历史摘要用）
TOOL_ACTION_LABELS: dict[str, str] = {
    "plan_iteration": "生成新计划",
    "replan_all": "全量重规划",
    "task_add": "新增任务",
    "task_update": "修改任务",
    "task_delete": "删除任务",
}

#: 可重规划覆盖字段 → 中文名
REPLAN_FIELD_LABELS: dict[str, str] = {
    "iterationDeadline": "截止日期",
    "iterationStartDate": "起始日期",
    "devMemberCount": "开发人数",
}

#: 可更新字段 → 中文名（与 ``UPDATABLE_FIELDS`` 一一对应，仅用于确认卡片展示）
UPDATABLE_FIELD_LABELS_MAP: dict[str, str] = {
    "estimatedHours": "预估工时",
    "predecessorTaskIds": "前置依赖",
    "status": "状态",
    "priority": "优先级",
    "assignee": "责任人",
}


def _short(value: object, limit: int = 36) -> str:
    """截断展示用文本（确认卡片单行展示，过长会被截断）。"""
    text = str(value).strip() if value is not None else ""
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def describe_action(name: str, args: dict[str, Any], *, task_name: str | None = None) -> str:
    """把待确认的写操作翻译为一句人读描述（服务端确定性生成，不依赖模型输出）。

    ``task_name`` 为已解析出的任务名（由 :class:`AgentRunner` 在登记前查计划取得）。
    仅有 ``taskId`` 时确认卡片只能展示一串无意义的 ID，用户无法判断改的是哪个任务；
    因此校验通过后一律同时展示「任务名 + ID」，让确认环节真正可核对。
    """
    action = TOOL_ACTION_LABELS.get(name, name)
    plan_ref = _short(args.get("planId"), 24) or "当前计划"
    if name == "plan_iteration":
        return f"{action}：{_short(args.get('requirement'))}"
    if name == "replan_all":
        overrides = [label for key, label in REPLAN_FIELD_LABELS.items() if args.get(key) is not None]
        suffix = f"（覆盖 {'、'.join(overrides)}）" if overrides else ""
        return f"{action}：{plan_ref}{suffix}"
    if name == "task_add":
        return f"{action}「{_short(args.get('taskName'), 28)}」（计划 {plan_ref}）"
    target = _short(args.get("taskId"), 20)
    if task_name:
        target = f"「{_short(task_name, 28)}」({target})"
    if name == "task_update":
        field = UPDATABLE_FIELD_LABELS_MAP.get(str(args.get("updateField") or ""), str(args.get("updateField") or ""))
        return f"{action} {target} 的 {field} → {_short(args.get('newValue'), 22)}"
    if name == "task_delete":
        return f"{action} {target}（计划 {plan_ref}）"
    return action


__all__ = [
    "TOOL_SPECS",
    "TOOL_NAMES",
    "WRITE_TOOL_NAMES",
    "WRITE_TOOLS",
    "RETRIEVAL_TOOL_NAMES",
    "RETRIEVAL_TOOLS",
    "TOOL_RESULT_PENDING_CONFIRMATION",
    "TOOL_ACTION_LABELS",
    "REPLAN_FIELD_LABELS",
    "UPDATABLE_FIELD_LABELS_MAP",
    "describe_action",
]
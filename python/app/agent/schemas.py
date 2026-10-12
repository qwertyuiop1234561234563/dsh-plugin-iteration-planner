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
    _fn("get_plan", "读取计划完整快照。", {"planId": {"type": "string"}}, ["planId"]),
    _fn("list_plans", "列出全部计划摘要。", {}),
    _fn(
        "task_add",
        "新增任务并自动全量重规划。",
        {
            "planId": {"type": "string"},
            "taskName": {"type": "string"},
            "taskDesc": {"type": "string"},
            "taskType": {"type": "string", "enum": TASK_TYPE_VALUES},
            "estimatedHours": {"type": "number", "exclusiveMinimum": 0},
            "predecessorTaskIds": {"type": "array", "items": {"type": "string"}},
            "status": {"type": "string", "enum": TASK_STATUS_VALUES},
            "priority": {"type": "string", "enum": PRIORITY_VALUES},
            "assignee": {"type": "string"},
        },
        ["planId", "taskName"],
    ),
    _fn(
        "task_update",
        "修改任务单字段并自动全量重规划。",
        {
            "planId": {"type": "string"},
            "taskId": {"type": "string"},
            "updateField": {"type": "string", "enum": UPDATABLE_FIELDS},
            "newValue": {"description": "新值；类型随 updateField 而定"},
        },
        ["planId", "taskId", "updateField", "newValue"],
    ),
    _fn(
        "task_delete",
        "删除任务并清理其它任务的依赖引用（自动重规划）。",
        {"planId": {"type": "string"}, "taskId": {"type": "string"}},
        ["planId", "taskId"],
    ),
    _fn("dag_build", "构建并读取 DAG 视图（节点 / 边 / 关键路径 / 环 / 孤立任务）。", {"planId": {"type": "string"}}, ["planId"]),
    _fn("dag_check", "执行 Kahn 拓扑校验，返回是否成环与环内任务。", {"planId": {"type": "string"}}, ["planId"]),
    _fn("schedule_calc", "读取关键路径与总工期（AOE 计算结果）。", {"planId": {"type": "string"}}, ["planId"]),
    _fn("risk_scan", "读取风险清单（循环依赖 / 人员过载 / 关键路径延期 / 孤立任务 / 语义风险）。", {"planId": {"type": "string"}}, ["planId"]),
    _fn("report_generate", "生成迭代报表（Markdown 文本 + 燃尽图原始数据）。", {"planId": {"type": "string"}}, ["planId"]),
]

TOOL_NAMES: list[str] = [spec["function"]["name"] for spec in TOOL_SPECS]

__all__ = ["TOOL_SPECS", "TOOL_NAMES"]
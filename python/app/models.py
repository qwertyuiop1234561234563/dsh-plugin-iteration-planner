"""Python 对外 API 的请求 / 响应模型。

仅覆盖 Python 原生端点（``/api/agent/*``、``/api/scenario/*``）；``/api/plans/**``
为透明转发，校验与信封由 TypeScript 引擎统一负责，避免两处校验规则漂移。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

_ISO_DATE = r"^\d{4}-\d{2}-\d{2}$"


class AgentPlanRequest(BaseModel):
    """自然语言需求 → 完整迭代计划（Agent 端到端编排）。"""

    requirement: str = Field(min_length=1, max_length=20_000, description="业务需求原文")
    iterationDeadline: str | None = Field(default=None, pattern=_ISO_DATE, description="迭代截止日期 YYYY-MM-DD")
    iterationStartDate: str | None = Field(default=None, pattern=_ISO_DATE, description="迭代起始日期 YYYY-MM-DD")
    devMemberCount: int | None = Field(default=None, gt=0, le=1000, description="可用开发人数")


class AgentChatRequest(BaseModel):
    """会话式对话（携带上下文，可多轮修改计划）。"""

    message: str = Field(min_length=1, max_length=20_000, description="用户消息")
    sessionId: str | None = Field(default=None, max_length=128, description="会话 ID；缺省则新建会话")


class ToolCallRecord(BaseModel):
    """单次工具调用记录（用于前端展示与审计）。"""

    name: str
    arguments: dict[str, Any]
    ok: bool
    result: Any | None = None
    error: str | None = None


class AgentRunResult(BaseModel):
    """Agent 一次运行的结构化输出。"""

    content: str
    planId: str | None = None
    toolCalls: list[ToolCallRecord] = Field(default_factory=list)
    steps: int = 0


class AgentChatResponse(AgentRunResult):
    """会话式对话响应（附带会话 ID）。"""

    sessionId: str


# --------------------------- 风险缓解建议（/api/agent/advise） ---------------------------


class AdviceRequest(BaseModel):
    """针对某计划的缓解建议请求。"""

    planId: str = Field(min_length=1, max_length=128, description="计划 ID")


class AdviceItem(BaseModel):
    """单条风险条目及其缓解动作。

    ``level`` / ``taskIds`` / ``description`` / ``suggestion`` 均**原样来自引擎**（风险识别
    的唯一事实来源）；``actions`` 才是本层新增内容（LLM 或规则派生的可执行步骤）。
    """

    riskId: str | None = None
    level: str
    taskIds: list[str] = Field(default_factory=list)
    description: str
    suggestion: str
    actions: list[str] = Field(default_factory=list)


class AdviceResponse(BaseModel):
    """缓解建议响应。

    ``mode`` 为 ``llm``（模型生成）或 ``rule``（规则降级）；``degradedReason`` 仅在
    模型不可用 / 输出不可解析而回退规则模式时填充。
    """

    mode: str = Field(description="llm | rule")
    summary: str
    items: list[AdviceItem] = Field(default_factory=list)
    model: str | None = None
    degradedReason: str | None = None


# --------------------------- What-if 情景对比（/api/scenario/compare） ---------------------------


class ScenarioConstraints(BaseModel):
    """场景约束（未提供的字段沿用基线计划取值）。"""

    iterationDeadline: str | None = Field(default=None, pattern=_ISO_DATE, description="迭代截止日期 YYYY-MM-DD")
    iterationStartDate: str | None = Field(default=None, pattern=_ISO_DATE, description="迭代起始日期 YYYY-MM-DD")
    devMemberCount: int | None = Field(default=None, gt=0, le=1000, description="可用开发人数")


class ScenarioCompareRequest(ScenarioConstraints):
    """情景对比请求：在基线计划需求不变的前提下改动约束重新规划。"""

    planId: str = Field(min_length=1, max_length=128, description="基线计划 ID")


class ScenarioMetrics(BaseModel):
    """单个场景的关键指标（全部来自引擎返回值）。"""

    totalDurationHours: float
    totalEstimatedHours: float
    taskCount: int
    criticalTaskCount: int
    riskCount: int
    highRiskCount: int
    plannedEndDate: str | None = None
    deadline: str | None = None
    deadlineMet: bool | None = None


class ScenarioDelta(BaseModel):
    """情景相对基线的差值（情景 − 基线）。"""

    totalDurationHours: float
    totalEstimatedHours: float
    taskCount: int
    criticalTaskCount: int
    riskCount: int
    highRiskCount: int


class ScenarioCompareResponse(BaseModel):
    """情景对比响应。"""

    planId: str
    baselineConstraints: ScenarioConstraints
    scenarioConstraints: ScenarioConstraints
    baseline: ScenarioMetrics
    scenario: ScenarioMetrics
    delta: ScenarioDelta
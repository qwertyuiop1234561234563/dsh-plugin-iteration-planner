"""Python 对外 API 的请求 / 响应模型。

仅覆盖 Python 原生端点（``/api/agent/*``、``/api/scenario/*``）；``/api/plans/**``
为透明转发，校验与信封由 TypeScript 引擎统一负责，避免两处校验规则漂移。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

_ISO_DATE = r"^\d{4}-\d{2}-\d{2}$"
#: 计划 ID 字符集（引擎生成 UUID）；限制字符集可避免 ID 被拼进上游 URL 时产生路径穿越
_ID = r"^[A-Za-z0-9_-]{1,128}$"


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
    planId: str | None = Field(
        default=None,
        max_length=64,
        description="前端当前查看的计划 ID（作为可信上下文注入，便于指代解析）",
    )
    deferWrites: bool = Field(
        default=True,
        description="写类工具是否需用户确认后执行（两阶段写操作）；置 false 则沿用模型直连执行",
    )


class ToolCallRecord(BaseModel):
    """单次工具调用记录（用于前端展示与审计）。"""

    name: str
    arguments: dict[str, Any]
    ok: bool
    result: Any | None = None
    error: str | None = None


class PendingAction(BaseModel):
    """待用户确认的写操作（两阶段写操作的第一阶段产物）。

    ``arguments`` 为模型给出的原始工具入参，**确认后才交给引擎执行**；``label`` 由服务端
    按工具元数据确定性生成，便于前端无需理解工具语义即可展示确认卡片。
    """

    actionId: str
    tool: str
    label: str
    arguments: dict[str, Any]
    planId: str | None = None


class AgentRunResult(BaseModel):
    """Agent 一次运行的结构化输出。"""

    content: str
    planId: str | None = None
    toolCalls: list[ToolCallRecord] = Field(default_factory=list)
    steps: int = 0
    pendingActions: list[PendingAction] = Field(default_factory=list)


class AgentChatResponse(AgentRunResult):
    """会话式对话响应（附带会话 ID）。"""

    sessionId: str


class AgentConfirmRequest(BaseModel):
    """确认（或驳回）待确认的写操作。"""

    sessionId: str = Field(min_length=1, max_length=128, description="会话 ID")
    actionIds: list[str] | None = Field(
        default=None,
        max_length=50,
        description="要执行的 actionId 列表；缺省表示确认全部待执行动作",
    )
    reject: bool = Field(
        default=False,
        description="驳回模式：作废全部待确认动作（不执行任何写操作），保留会话上下文",
    )


class AgentConfirmResponse(BaseModel):
    """确认执行结果（含未确认而保留的剩余动作）。"""

    sessionId: str
    planId: str | None = None
    executed: list[ToolCallRecord] = Field(default_factory=list)
    remainingActions: list[PendingAction] = Field(default_factory=list)
    #: 是否走了驳回模式（true 表示本轮未执行任何写操作）
    rejected: bool = False


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


# --------------------------- 治理端点（/api/workspace/**） ---------------------------
#
# 说明：``/api/workspace/**`` 为本层**新增的治理能力**，响应体以普通 dict 直接组装
# （与引擎 ``{"data": ...}`` 信封一致）。此处仅定义**入参模型**，因为入参需要严格
# 结构校验与字段级错误提示；出参是对引擎返回值 + 治理存储的只读聚合，用 dict 表达
# 更贴近实际形状，避免为一次性形状重复维护模型。


class ImportRow(BaseModel):
    """导入的一行任务（可来自 CSV 或 JSON，均由前端解析为结构化行）。"""

    taskName: str = Field(min_length=1, max_length=200, description="任务名称（必填）")
    taskDesc: str | None = Field(default=None, max_length=2000, description="任务描述")
    taskType: str | None = Field(default=None, description="任务类型，取值须来自引擎枚举")
    estimatedHours: float | None = Field(default=None, description="预估工时（小时，须为正数；非法值由行级校验统一报错）")
    predecessorRefs: list[str] = Field(default_factory=list, description="前置引用：既可为已存在任务的 taskId，也可为本批次其他行的任务名称")
    status: str | None = Field(default=None, description="任务状态，取值须来自引擎枚举")
    priority: str | None = Field(default=None, description="优先级，取值须来自引擎枚举")
    assignee: str | None = Field(default=None, max_length=120, description="责任人")


class ImportRequest(BaseModel):
    """数据导入请求：``append`` 追加到现有计划，``new`` 由导入行新建计划。"""

    mode: str = Field(default="append", pattern="^(append|new)$", description="append | new")
    planId: str | None = Field(default=None, pattern=_ID, description="mode=append 时的目标计划 ID")
    requirement: str | None = Field(default=None, min_length=1, max_length=20_000, description="mode=new 时的需求原文")
    iterationDeadline: str | None = Field(default=None, pattern=_ISO_DATE, description="mode=new 时的迭代截止日期")
    iterationStartDate: str | None = Field(default=None, pattern=_ISO_DATE, description="mode=new 时的迭代起始日期")
    devMemberCount: int | None = Field(default=None, gt=0, le=1000, description="mode=new 时的开发人数")
    rows: list[ImportRow] = Field(min_length=1, max_length=500, description="导入行")


class BaselineCreateRequest(BaseModel):
    """创建基线快照（快照内容由服务端从引擎读取，不接受前端传入计划体）。"""

    name: str = Field(min_length=1, max_length=120, description="基线名称")
    note: str | None = Field(default=None, max_length=500, description="备注")


class IssueCreateRequest(BaseModel):
    """人工风险登记册：新建条目。"""

    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    severity: str = Field(default="MEDIUM", description="LOW | MEDIUM | HIGH | CRITICAL")
    probability: str = Field(default="MEDIUM", description="LOW | MEDIUM | HIGH")
    owner: str | None = Field(default=None, max_length=120)
    mitigation: str | None = Field(default=None, max_length=2000)
    status: str = Field(default="OPEN", description="OPEN | MITIGATING | MONITORING | CLOSED | ACCEPTED")
    dueDate: str | None = Field(default=None, pattern=_ISO_DATE)
    linkedTaskIds: list[str] = Field(default_factory=list)


class IssueUpdateRequest(BaseModel):
    """人工风险登记册：部分更新（仅提交的字段生效）。"""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    severity: str | None = None
    probability: str | None = None
    owner: str | None = Field(default=None, max_length=120)
    mitigation: str | None = Field(default=None, max_length=2000)
    status: str | None = None
    dueDate: str | None = Field(default=None, pattern=_ISO_DATE)
    linkedTaskIds: list[str] | None = None


class PreferencesRequest(BaseModel):
    """配置备份写入：payload 为前端 localStorage 中可备份的偏好快照。"""

    profile: str | None = Field(default=None, max_length=64)
    payload: dict[str, Any]
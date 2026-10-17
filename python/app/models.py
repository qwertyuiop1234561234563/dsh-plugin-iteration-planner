"""Python 对外 API 的请求 / 响应模型。

仅覆盖 Python 原生端点（``/api/agent/*``、``/api/scenario/*``）；``/api/plans/**``
为透明转发，校验与信封由 TypeScript 引擎统一负责，避免两处校验规则漂移。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .constants import (
    MAX_PLAN_ASSIGNMENTS,
    PASSWORD_MAX_LEN,
    PASSWORD_MIN_LEN,
    ROLES,
    USERNAME_MAX_LEN,
    USERNAME_MIN_LEN,
)

_ISO_DATE = r"^\d{4}-\d{2}-\d{2}$"
#: 标识符字符集（计划 ID / 会话 ID：引擎与服务端均生成 UUID，仅接受字母数字与 ``-`` / ``_``）。
#: 限制字符集可避免 ID 被拼进上游 URL 时产生路径穿越，也避免任意形态的键污染会话存储。
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
    sessionId: str | None = Field(
        default=None,
        pattern=_ID,
        description="会话 ID；缺省则新建会话（服务端下发 uuid4().hex，原样回传）",
    )
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
    #: 本轮实际引用到的知识库片段（供前端展示「参考依据」，便于用户核对模型结论来源）
    references: list["KbHit"] = Field(default_factory=list)
    #: 本轮注入上下文的长期记忆（供前端展示「模型记得什么」，便于用户纠偏）
    memories: list["MemoryItem"] = Field(default_factory=list)


class AgentChatResponse(AgentRunResult):
    """会话式对话响应（附带会话 ID）。"""

    sessionId: str


class AgentConfirmRequest(BaseModel):
    """确认（或驳回）待确认的写操作。"""

    sessionId: str = Field(min_length=1, max_length=128, pattern=_ID, description="会话 ID")
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


# ============================ 访问控制（/api/auth/**） ============================


class LoginRequest(BaseModel):
    """账号口令登录（成功后服务端下发 httpOnly 会话 Cookie）。"""

    username: str = Field(min_length=1, max_length=USERNAME_MAX_LEN, description="用户名（大小写不敏感）")
    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LEN, description="口令")


class ChangePasswordRequest(BaseModel):
    """当前登录账号修改自身口令（须校验原口令）。"""

    currentPassword: str = Field(min_length=1, max_length=PASSWORD_MAX_LEN, description="当前口令")
    newPassword: str = Field(
        min_length=PASSWORD_MIN_LEN,
        max_length=PASSWORD_MAX_LEN,
        description="新口令（需同时包含字母与数字）",
    )


class UserCreateRequest(BaseModel):
    """管理员新建账号。"""

    username: str = Field(min_length=USERNAME_MIN_LEN, max_length=USERNAME_MAX_LEN, description="用户名")
    password: str = Field(min_length=PASSWORD_MIN_LEN, max_length=PASSWORD_MAX_LEN, description="初始口令")
    role: str = Field(default="viewer", description=f"角色，可选：{' / '.join(ROLES)}")
    displayName: str | None = Field(default=None, max_length=64, description="显示名（缺省与用户名相同）")


class UserUpdateRequest(BaseModel):
    """管理员更新账号（仅提交需要变更的字段）。"""

    role: str | None = Field(default=None, description=f"角色，可选：{' / '.join(ROLES)}")
    status: str | None = Field(default=None, description="账号状态，可选：active / disabled")
    displayName: str | None = Field(default=None, max_length=64, description="显示名")
    password: str | None = Field(
        default=None,
        min_length=PASSWORD_MIN_LEN,
        max_length=PASSWORD_MAX_LEN,
        description="重置口令（提交后该账号的既有会话立即失效）",
    )


class PlanAssignmentsRequest(BaseModel):
    """设置访客（guest）可见的计划白名单（**全量覆盖**语义）。"""

    planIds: list[str] = Field(
        default_factory=list,
        max_length=MAX_PLAN_ASSIGNMENTS,
        description="可见计划 ID 列表；空数组表示无权查看任何计划",
    )


# ============================ 知识库（RAG） ============================
#
# 说明：知识库与长期记忆是本层**新增的检索增强能力**，出参形状稳定且前端强依赖
# （列表渲染、字段级提示），因此与既有 ``/api/workspace/**`` 的「dict 组装」不同，
# 这里给出显式模型，保证前后端契约可被 IDE 与类型检查校验。


class KbDocumentCreateRequest(BaseModel):
    """以「直接粘贴文本」的方式新建知识库文档（与文件上传接口互补）。"""

    title: str = Field(min_length=1, max_length=200, description="文档标题")
    content: str = Field(min_length=1, max_length=2_000_000, description="文档正文（纯文本 / Markdown）")
    source: str | None = Field(default=None, max_length=200, description="来源说明（如系统名、链接）")
    tags: list[str] = Field(default_factory=list, max_length=10, description="标签")


class KbSearchRequest(BaseModel):
    """知识库检索（混合检索试跑 / 外部系统调用）。"""

    query: str = Field(min_length=1, max_length=2000, description="检索关键词或问题")
    topK: int | None = Field(default=None, gt=0, le=50, description="返回条数（缺省取服务端配置）")
    hybrid: bool | None = Field(default=None, description="是否启用混合检索（缺省取服务端配置）")


class KbHit(BaseModel):
    """单条命中片段（``score`` 为 RRF 融合分或余弦相似度）。"""

    chunkId: str | None = None
    docId: str | None = None
    title: str | None = None
    source: str = ""
    chunkIndex: int = 0
    tags: list[str] = Field(default_factory=list)
    text: str = ""
    score: float = 0.0


class KbHitList(BaseModel):
    """命中片段列表。"""

    query: str
    mode: str = Field(description="hybrid | dense")
    items: list[KbHit] = Field(default_factory=list)


class KbChunkPreview(BaseModel):
    """分块预览（文档详情用）。"""

    chunkId: str | None = None
    chunkIndex: int = 0
    text: str = ""


class KbDocument(BaseModel):
    """知识库文档摘要。"""

    docId: str
    title: str | None = None
    source: str = ""
    filename: str = ""
    extension: str = ""
    tags: list[str] = Field(default_factory=list)
    chunkCount: int = 0
    charCount: int = 0
    createdAt: str = ""


class KbDocumentDetail(KbDocument):
    """知识库文档详情（含分块预览）。"""

    chunks: list[KbChunkPreview] = Field(default_factory=list)


class KbDocumentList(BaseModel):
    """文档列表。"""

    items: list[KbDocument] = Field(default_factory=list)
    total: int = 0


class KbDeleteResult(BaseModel):
    """文档删除结果。"""

    docId: str
    deletedChunks: int = 0
    deleted: bool = True


class KbStats(BaseModel):
    """知识库统计与检索配置（前端「设置概览」展示）。"""

    documentCount: int = 0
    chunkCount: int = 0
    totalChars: int = 0
    hybrid: bool = True
    embedder: str = ""
    chunkSize: int = 0
    chunkOverlap: int = 0
    topK: int = 0
    maxDocuments: int = 0
    updatedAt: str | None = None


# ============================ 长期记忆 ============================


class MemoryCreateRequest(BaseModel):
    """手工新增一条长期记忆（自动提取之外的治理入口）。"""

    text: str = Field(min_length=1, max_length=500, description="记忆内容（自包含的一句话）")
    type: str = Field(default="fact", description="fact | preference | decision | constraint | task | other")
    importance: str = Field(default="medium", description="high | medium | low")
    tags: list[str] = Field(default_factory=list, max_length=5)
    source: str | None = Field(default=None, max_length=128, description="来源标记（会话 ID 或手工）")


class MemoryStatusRequest(BaseModel):
    """启用 / 停用一条记忆。"""

    status: str = Field(description="active | disabled")


class MemoryItem(BaseModel):
    """单条长期记忆。"""

    memoryId: str
    text: str = ""
    type: str = "other"
    importance: str = "medium"
    tags: list[str] = Field(default_factory=list)
    source: str = ""
    status: str = "active"
    createdAt: str = ""
    lastUsedAt: str = ""
    score: float = 0.0
    #: 写入是否真正发生（``false`` 表示与既有记忆高度相似被去重跳过）
    created: bool = True


class MemoryList(BaseModel):
    """记忆列表。"""

    items: list[MemoryItem] = Field(default_factory=list)
    total: int = 0


class MemorySearchRequest(BaseModel):
    """按语义召回记忆（试跑 / 外部系统调用）。"""

    query: str = Field(min_length=1, max_length=2000)
    topK: int | None = Field(default=None, gt=0, le=50)


class MemorySearchResult(BaseModel):
    """记忆召回结果。"""

    query: str
    items: list[MemoryItem] = Field(default_factory=list)


class MemoryStats(BaseModel):
    """记忆统计。"""

    total: int = 0
    active: int = 0
    disabled: int = 0
    byType: dict[str, int] = Field(default_factory=dict)
    maxItems: int = 0
    autoExtract: bool = False
    topK: int = 0


# ``AgentRunResult`` 通过**前向引用**引用知识库 / 记忆模型（声明位置在文件后部），
# 需在模块加载完成后显式重建，否则首次校验时才会解析，易在子类继承场景下遗漏。
AgentRunResult.model_rebuild()
AgentChatResponse.model_rebuild()
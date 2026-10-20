/**
 * 前端领域类型定义。
 *
 * 与后端 `src/models/entities.ts`、`src/api/graph.ts` 的数据契约保持一致；
 * 枚举取值与后端 `src/constants/enums.ts` 单一事实来源对齐。
 */

/* --------------------------------- 枚举 --------------------------------- */

export const TASK_TYPE_VALUES = [
  'DB_DESIGN',
  'API_DEV',
  'BUSINESS_CODE',
  'DEBUG',
  'TEST',
  'DEPLOY',
  'DOC',
] as const
export type TaskType = (typeof TASK_TYPE_VALUES)[number]

export const TASK_STATUS_VALUES = ['TODO', 'IN_PROGRESS', 'BLOCKED', 'TESTING', 'DONE', 'DELAYED'] as const
export type TaskStatus = (typeof TASK_STATUS_VALUES)[number]

export const PRIORITY_VALUES = ['P0', 'P1', 'P2', 'P3'] as const
export type Priority = (typeof PRIORITY_VALUES)[number]

export const RISK_LEVEL_VALUES = ['HIGH', 'MEDIUM', 'LOW'] as const
export type RiskLevel = (typeof RISK_LEVEL_VALUES)[number]

/** 人工风险登记册：严重度取值（与 Python `app.constants.ISSUE_SEVERITIES` 对齐） */
export const ISSUE_SEVERITY_VALUES = ['LOW', 'MEDIUM', 'HIGH', 'CRITICAL'] as const
export type IssueSeverity = (typeof ISSUE_SEVERITY_VALUES)[number]

/** 人工风险登记册：发生概率取值 */
export const ISSUE_PROBABILITY_VALUES = ['LOW', 'MEDIUM', 'HIGH'] as const
export type IssueProbability = (typeof ISSUE_PROBABILITY_VALUES)[number]

/** 人工风险登记册：处置状态取值 */
export const ISSUE_STATUS_VALUES = ['OPEN', 'MITIGATING', 'MONITORING', 'CLOSED', 'ACCEPTED'] as const
export type IssueStatus = (typeof ISSUE_STATUS_VALUES)[number]

/** 仍视为「未关闭」的处置状态（与 Python `ISSUE_OPEN_STATUSES` 对齐） */
export const ISSUE_OPEN_STATUSES: readonly IssueStatus[] = ['OPEN', 'MITIGATING', 'MONITORING']

/** 高关注严重度（与 Python `ISSUE_HIGH_SEVERITIES` 对齐） */
export const ISSUE_HIGH_SEVERITIES: readonly IssueSeverity[] = ['HIGH', 'CRITICAL']

/** 数据导入模式：追加到现有计划 / 由导入行新建计划 */
export const IMPORT_MODE_VALUES = ['append', 'new'] as const
export type ImportMode = (typeof IMPORT_MODE_VALUES)[number]

/** 任务可更新字段白名单（与后端 §四.5 对齐） */
export const UPDATABLE_FIELDS = ['estimatedHours', 'predecessorTaskIds', 'status', 'priority', 'assignee'] as const
export type UpdatableField = (typeof UPDATABLE_FIELDS)[number]

/* --------------------------------- 实体 --------------------------------- */

export interface Task {
  taskId: string
  taskName: string
  taskDesc: string
  taskType: TaskType
  estimatedHours: number
  predecessorTaskIds: string[]
  status: TaskStatus
  priority: Priority
  assignee?: string
  createTime: number
  updateTime: number
  /** 最早开始时间（小时，相对迭代起点） */
  es: number
  /** 最晚开始时间（小时，相对迭代起点） */
  ls: number
  /** 松弛时间；slack=0 即关键路径任务 */
  slack: number
}

export interface RiskItem {
  riskId: string
  level: RiskLevel
  taskIds: string[]
  description: string
  suggest: string
}

export interface BurndownItem {
  time: string
  remainingHours: number
}

export interface ProjectPlan {
  planId: string
  rawRequirement: string
  iterationDeadline: string
  iterationStartDate: string
  devMemberCount: number
  taskList: Task[]
  riskList: RiskItem[]
  criticalPath: string[]
  burndownRawData: BurndownItem[]
  totalEstimatedHours: number
  totalDurationHours: number
}

export interface PlanSummary {
  planId: string
  rawRequirement: string
  iterationDeadline: string
  iterationStartDate: string
  devMemberCount: number
  taskCount: number
  riskCount: number
  highRiskCount: number
  totalEstimatedHours: number
  totalDurationHours: number
  createdAt: number
  updatedAt: number
}

/* ------------------------------- DAG 视图 ------------------------------- */

export interface GraphNode {
  id: string
  label: string
  taskType: TaskType
  priority: Priority
  status: TaskStatus
  assignee?: string
  estimatedHours: number
  es: number
  ls: number
  slack: number
  critical: boolean
  isolated: boolean
  inCycle: boolean
}

export interface GraphEdge {
  source: string
  target: string
}

export interface GraphPayload {
  nodes: GraphNode[]
  edges: GraphEdge[]
  hasCycle: boolean
  cycleNodes: string[]
  topoOrder: string[]
  isolatedTasks: string[]
  dangling: Array<{ taskId: string; missing: string[] }>
}

/* --------------------------------- 审计 --------------------------------- */

export interface AuditRecord {
  recordId: string
  planId?: string
  ts: string
  action: string
  ok: boolean
  durationMs: number
  input?: unknown
  output?: unknown
  error?: string
}

/* ------------------------------- 元数据 -------------------------------- */

export interface BufferRuleMeta {
  key: string
  label: string
  ratio: number
}

export interface EnumMeta {
  taskType: TaskType[]
  taskStatus: TaskStatus[]
  priority: Priority[]
  bufferRules: BufferRuleMeta[]
  updatableFields: UpdatableField[]
}

/* ------------------------------ 请求入参 ------------------------------- */

export interface CreatePlanPayload {
  requirement: string
  iterationDeadline: string
  iterationStartDate?: string
  devMemberCount: number
}

export interface TaskDraftPayload {
  taskName: string
  taskDesc?: string
  taskType?: TaskType
  estimatedHours?: number
  predecessorTaskIds?: string[]
  status?: TaskStatus
  priority?: Priority
  assignee?: string
}

export interface ReplanPayload {
  iterationDeadline?: string
  iterationStartDate?: string
  devMemberCount?: number
}

export interface TaskUpdatePayload {
  updateField: UpdatableField
  newValue: unknown
}

/* ------------------------------ 响应包装 ------------------------------- */

/** 后端统一成功响应包装 */
export interface ApiEnvelope<T> {
  data: T
}

/** 后端统一错误响应体 */
export interface ApiErrorBody {
  code: string
  message: string
  details?: unknown
}

/** 任务变更接口返回 */
export interface TaskMutationResult {
  task: Task
  plan: ProjectPlan
}

/** 删除任务接口返回 */
export interface DeleteTaskResult {
  plan: ProjectPlan
}

/** 报表接口返回 */
export interface ReportResult {
  format: 'markdown'
  content: string
  burndownRawData: BurndownItem[]
}

/** 健康检查返回 */
export interface HealthResult {
  status: string
  version: string
  storage: string
  uptimeMs: number
  wsSubscribers: number
}

/** WebSocket 推送事件（计划变更；巡检发现见 :interface:`PatrolFindingEvent`） */
export interface PlanEventMessage {
  type: 'connected' | 'plan.created' | 'plan.updated' | 'plan.deleted'
  planId?: string
  at: string
  reason?: string
  plan?: ProjectPlan
}

/* ------------------------- 关键路径 / 拓扑校验（引擎） ------------------------- */

export interface CriticalPathResult {
  criticalPath: string[]
  totalDurationHours: number
}

export interface DagCheckResult {
  hasCycle: boolean
  topoOrder: string[]
}

/* ------------------------------- Agent 能力 ------------------------------- */

export interface AgentHealth {
  enabled: boolean
  llm: string | null
  tools: string[]
  /** 需用户确认后才执行的写类工具（两阶段写操作） */
  writeTools: string[]
  /** 知识库（向量检索）是否已启用；未启用时不含 kb_* 工具，前端不展示引用卡片 */
  knowledgeEnabled?: boolean
  /** 长期记忆是否已启用；未启用时不含 memory_* 工具 */
  memoryEnabled?: boolean
  /** 是否在每轮对话后自动提取长期记忆 */
  memoryAutoExtract?: boolean
  /** 当前向量化模型名（未启用为 null） */
  embeddingModel?: string | null
}

/** 风险缓解建议来源：llm（模型生成）/ rule（规则聚合，LLM 未配置或调用失败时降级） */
export type AdviceMode = 'llm' | 'rule'

export interface AdviceItem {
  riskId: string
  level: RiskLevel
  taskIds: string[]
  description: string
  /** 引擎给出的原始缓解建议（权威口径） */
  suggestion: string
  /** 结构化行动项（模型生成或规则补充） */
  actions: string[]
}

export interface AdviceResult {
  mode: AdviceMode
  summary: string
  items: AdviceItem[]
  /** 生成建议所用模型（rule 模式为 null） */
  model: string | null
  /** 降级原因（mode=rule 且因 LLM 异常降级时填充） */
  degradedReason?: string
}

/* --------------------------- Agent 会话式对话（AI 助手） --------------------------- */

/** 会话式对话请求（写操作默认登记为待确认，确认后才落库） */
export interface AgentChatRequest {
  message: string
  /** 会话 ID；缺省由服务端新建 */
  sessionId?: string
  /** 当前查看的计划 ID（随对话上报，供模型解析「这个计划」这类指代） */
  planId?: string
  /** 写类工具是否需要用户确认（默认 true） */
  deferWrites?: boolean
}

/** 单次工具调用记录（用于过程展示与审计） */
export interface AgentToolCallRecord {
  name: string
  arguments: Record<string, unknown>
  ok: boolean
  result?: unknown
  error?: string | null
  /** 稳定错误码（供前端分流与自反思归因；失败调用才有） */
  code?: string | null
}

/**
 * 自反思事件（plan→act→observe→reflect 闭环的一次「自我诊断」）。
 *
 * 类别与策略由**服务端规则**确定性产出（错误码 → 类别），不依赖模型自述，
 * 因此可以在评测里作为断言对象、在界面上直接展示。
 */
export interface AgentReflection {
  /** 本次运行为第几条反思（从 1 开始） */
  index: number
  /** 触发该反思的编排步号 */
  step: number
  /** 出问题的工具名 */
  tool: string
  /** 归因类别：invalid_arguments / not_found / dependency_cycle / engine_unavailable / format_error / empty_result / unknown */
  category: string
  /** 采取的恢复策略：retry_with_args / reroute_tool / degrade / ask_user */
  strategy: string
  /** 同一（工具 + 类别）签名的第几次尝试；超过上限即熔断降级 */
  attempt: number
  signature: string
  title: string
  detail: string
  /** 是否已恢复（后续同工具调用成功即回填为 true） */
  recovered: boolean
  at: string
}

/** 待确认的写操作（两阶段写操作的第一阶段产物） */
export interface AgentPendingAction {
  actionId: string
  /** 工具名（plan_iteration / replan_all / task_add / task_update / task_delete） */
  tool: string
  /** 服务端生成的人读描述（确认卡片直接展示） */
  label: string
  arguments: Record<string, unknown>
  planId?: string | null
}

/** 会话式对话响应 */
export interface AgentChatResult {
  sessionId: string
  content: string
  planId?: string | null
  toolCalls: AgentToolCallRecord[]
  steps: number
  pendingActions: AgentPendingAction[]
  /** 本轮自反思轨迹（未启用反思时为空数组） */
  reflections?: AgentReflection[]
  /** 本条运行台账 ID（有待确认动作或编排失败时才有；据此可在重启后继续） */
  checkpointId?: string | null
  /** 本条执行轨迹 ID（每次运行都有；据此可打开瀑布图 / 逐 step 回放 / 沉淀回归用例） */
  traceId?: string | null
  /** 一致性护栏触发次数（谎报完成 / 无依据作答 / 写指令空转被拦）；>0 表示本轮被纠正过 */
  guardCount?: number
  /** 输出溯源报告：逐条事实性声明的引用与未核实计数（未启用为 null） */
  provenance?: ProvenanceReport | null
  /** 本轮上下文分区预算明细（未启用为 null） */
  budget?: BudgetReport | null
  /** 本轮实际引用到的知识库片段（供用户核对结论依据） */
  references?: KbHit[]
  /** 本轮注入上下文的长期记忆（供用户纠偏） */
  memories?: MemoryItem[]
}

/** 流式对话首帧：会话 ID 与本轮检索增强上下文（知识库引用 / 长期记忆） */
export interface AgentStreamMeta {
  sessionId: string
  references: KbHit[]
  memories: MemoryItem[]
}

/** 流式工具调用结果（轻量：不含完整 result，最终事件会补齐完整明细） */
export interface AgentStreamToolResult {
  name: string
  arguments: Record<string, unknown>
  ok: boolean
  error?: string | null
}

/** 确认执行待确认写操作的响应 */
export interface AgentConfirmResult {
  sessionId: string
  planId?: string | null
  executed: AgentToolCallRecord[]
  /** 未确认（保留）的待确认动作 */
  remainingActions: AgentPendingAction[]
  /** 是否走了驳回模式（true 表示本轮未执行任何写操作） */
  rejected: boolean
}

/* ------------------------- Agent 高级能力（协作 / 沙盘 / 复盘 / 巡检） ------------------------- */

/* ---- 多角色协作（plan-act-observe：每步可见、可中断） ---- */

/** 多角色协作请求：一句话需求 → 需求分析 / 引擎排期 / 估算审阅 / 风险缓解 / 质量校验 */
export interface AgentCollabRequest {
  requirement: string
  iterationDeadline?: string
  iterationStartDate?: string
  devMemberCount?: number
}

/** 协作角色步骤（前端时间线；status 与后端 STEP_* 常量对齐） */
export type AgentCollabStepStatus = 'pending' | 'running' | 'done' | 'failed' | 'skipped'

export interface AgentCollabStep {
  role: string
  title: string
  status: AgentCollabStepStatus | string
  detail: string
  at: number
}

/* ---- What-if 沙盘（自然语言仿真，临时计划即时回收） ---- */

export interface WhatIfTaskEdit {
  task: string
  taskId?: string | null
  field: string
  value?: unknown
  before?: unknown
  after?: unknown
  applied: boolean
  error?: string | null
  reason?: string | null
}

export interface WhatIfRequest {
  planId: string
  instruction: string
}

export interface WhatIfResult {
  planId: string
  /** llm | rule（解析路径；rule 表示未用模型，附带 degradedReason） */
  mode: AdviceMode
  model: string | null
  degradedReason?: string
  /** 服务端理解到的情景意图（回显给用户核对） */
  intent: string
  constraints: ScenarioConstraints
  baselineConstraints: ScenarioConstraints
  baseline: ScenarioMetrics
  scenario: ScenarioMetrics
  delta: ScenarioDelta
  /** 预计完成日期偏移天数（正值为推迟） */
  deadlineShiftDays?: number | null
  criticalPathAdded: string[]
  criticalPathRemoved: string[]
  newRisks: string[]
  resolvedRisks: string[]
  edits: WhatIfTaskEdit[]
  /** 描述无法映射为任何可执行改动 */
  unparsed: boolean
  summary: string
  notes: string[]
  /** 情景计划已即时回收，不污染持久化存储 */
  ephemeral: boolean
}

/* ---- 迭代复盘 + 学习 ---- */

export interface RetroRequest {
  planId: string
  /** 实际工时映射（键为 taskId 或任务名）；不提供则跳过工时偏差与估算校准 */
  actualHours?: Record<string, number>
  /** 人工判定「确实发生了」的风险 ID */
  materializedRiskIds?: string[]
  /** 相对基线的范围变更说明 */
  scopeChanges?: string[]
  /** 是否把复盘结论写回长期记忆 */
  writeMemory?: boolean
}

export interface RetroResult {
  planId: string
  /** 确定性统计（可复算；缺数据的分区带 available=false 与原因） */
  metrics: Record<string, unknown>
  mode: AdviceMode
  model: string | null
  degradedReason?: string
  summary: string
  highlights: string[]
  problems: string[]
  actions: string[]
  memories: string[]
  /** 实际写回长期记忆的条数（去重后可能少于 memories） */
  memoryWritten: number
  memoryEnabled: boolean
}

/* ---- 主动巡检 Agent ---- */

export type PatrolSeverity = 'info' | 'warning' | 'critical'

/** 一条巡检发现（信号 + 严重度 + 可读描述，全部源于引擎事实） */
export interface PatrolFinding {
  findingId: string
  planId: string
  planRequirement: string
  signal: string
  signalLabel: string
  severity: PatrolSeverity | string
  title: string
  detail: string
  /** new | ongoing | resolved */
  status: string
  escalated: boolean
  /** 发现时间（Unix 秒，与引擎事件的时间口径一致） */
  at: number
}

export interface PatrolFindingsResult {
  items: PatrolFinding[]
  total: number
  running: boolean
  enabled: boolean
  intervalSeconds: number
  lastRunAt: number | null
  lastSummary: Record<string, unknown>
  signals: string[]
}

export interface PatrolRunResult {
  plans: number
  signals: number
  alerts: number
  resolved: number
  errors: string[]
  durationMs: number
  at: number | null
}

export interface PatrolSignalInfo {
  name: string
  label: string
}

export interface PatrolSignalsResult {
  signals: PatrolSignalInfo[]
  severities: string[]
}

/** 巡检发现的 WebSocket 推送载荷（携带 planId，受限连接按可见范围过滤） */
export interface PatrolFindingEvent extends PatrolFinding {
  type: 'patrol.finding'
}

/* ------------------------------ What-if 情景 ----------------------------- */

export interface ScenarioConstraints {
  iterationDeadline?: string
  iterationStartDate?: string
  devMemberCount?: number
}

/** 单次规划的关键指标（全部取自引擎返回值，前端仅做展示换算） */
export interface ScenarioMetrics {
  totalDurationHours: number
  totalEstimatedHours: number
  taskCount: number
  criticalTaskCount: number
  riskCount: number
  highRiskCount: number
  /** 引擎燃尽数据推导的计划完成日期（ISO） */
  plannedEndDate: string | null
  /** 约束中的迭代截止日期（ISO） */
  deadline: string | null
  /** 计划完成日期是否不晚于截止日期（null 表示无法判定） */
  deadlineMet: boolean | null
}

export interface ScenarioDelta {
  totalDurationHours: number
  totalEstimatedHours: number
  taskCount: number
  criticalTaskCount: number
  riskCount: number
  highRiskCount: number
}

export interface ScenarioCompareRequest extends ScenarioConstraints {
  planId: string
}

export interface ScenarioCompareResult {
  planId: string
  baselineConstraints: Required<ScenarioConstraints>
  scenarioConstraints: Required<ScenarioConstraints>
  baseline: ScenarioMetrics
  scenario: ScenarioMetrics
  delta: ScenarioDelta
}

/* ------------------------------ 数据导入（治理层） ------------------------------ */

/** 导入的一行任务（前端把 CSV / 文本解析为该结构后提交） */
export interface ImportRowPayload {
  taskName: string
  taskDesc?: string
  taskType?: string
  estimatedHours?: number
  predecessorRefs?: string[]
  status?: string
  priority?: string
  assignee?: string
}

export interface ImportPayload {
  mode: ImportMode
  /** mode=append 时的目标计划 */
  planId?: string
  /** mode=new 时的需求原文 */
  requirement?: string
  iterationDeadline?: string
  iterationStartDate?: string
  devMemberCount?: number
  rows: ImportRowPayload[]
}

export interface ImportedTaskRef {
  ref: string
  taskId: string
  taskName: string
}

export interface ImportResult {
  planId: string
  mode: ImportMode
  addedCount: number
  tasks: ImportedTaskRef[]
  createdPlan: boolean
}

/** 导入失败时后端返回的行级错误（details 中可能为 `errors` 数组，也可能是字段级校验列表） */
export interface ImportRowError {
  row: number
  field: string
  message: string
}

/* ---------------------------- 基线快照与偏差对比 ---------------------------- */

/** 计划标量摘要（基线列表与偏差对比共用口径，全部来自引擎返回值） */
export interface PlanMetrics {
  taskCount: number
  criticalTaskCount: number
  riskCount: number
  highRiskCount: number
  totalEstimatedHours: number
  totalDurationHours: number
  plannedEndDate: string | null
}

export interface BaselineSummary extends PlanMetrics {
  baselineId: string
  planId: string
  name: string
  note?: string | null
  createdAt: string
}

export interface BaselineDetail extends BaselineSummary {
  taskList: Task[]
  riskList: RiskItem[]
}

export interface BaselineMetricDiff {
  from: number | string | null
  to: number | string | null
  delta: number | null
}

export interface BaselineTaskRef {
  taskId: string
  taskName?: string
  estimatedHours?: number
  status?: string
  priority?: string
}

export interface BaselineChangedTask {
  taskId: string
  taskName?: string
  fields: Array<{ field: string; from: unknown; to: unknown }>
}

export interface BaselineDiff {
  baseline: BaselineSummary
  currentPlanId: string
  currentCapturedAt: string
  added: BaselineTaskRef[]
  removed: BaselineTaskRef[]
  changed: BaselineChangedTask[]
  metrics: Record<string, BaselineMetricDiff>
}

/* ------------------------------ 人工风险登记册 ------------------------------ */

export interface RegisterItem {
  issueId: string
  planId: string
  title: string
  description?: string | null
  severity: IssueSeverity
  probability: IssueProbability
  owner?: string | null
  mitigation?: string | null
  status: IssueStatus
  dueDate?: string | null
  linkedTaskIds: string[]
  createdAt: string
  updatedAt: string
}

export interface RegisterStats {
  total: number
  open: number
  highOpen: number
  severities: Record<string, number>
  statuses: Record<string, number>
}

export interface RegisterListResult {
  planId: string
  items: RegisterItem[]
  stats: RegisterStats
}

export interface RegisterPayload {
  title: string
  description?: string
  severity: IssueSeverity
  probability: IssueProbability
  owner?: string
  mitigation?: string
  status: IssueStatus
  dueDate?: string
  linkedTaskIds?: string[]
}

/* -------------------------------- 人员负荷 -------------------------------- */

export interface WorkloadMember {
  assignee: string
  taskCount: number
  criticalCount: number
  estimatedHours: number
  remainingHours: number
  capacityHours: number
  loadRatio: number
  overloaded: boolean
}

export interface UnassignedTaskRef {
  taskId: string
  taskName: string
  estimatedHours: number
  priority?: string
  critical: boolean
}

export interface WorkloadResult {
  planId: string
  hoursPerDay: number
  planWorkdays: number
  capacityHoursPerMember: number
  members: WorkloadMember[]
  unassigned: { taskCount: number; estimatedHours: number; tasks: UnassignedTaskRef[] }
  totals: { memberCount: number; capacityHours: number; allocatedHours: number; overloadedMembers: string[] }
}

/* ------------------------------ 燃尽分析（增强） ------------------------------ */

export interface BurndownActualPoint {
  time: string
  remainingHours: number
  statusCounts: Record<string, number>
}

export interface BurndownAnalysis {
  planId: string
  ideal: BurndownItem[]
  actual: BurndownActualPoint[]
  recordCount: number
  burnRatePerDay: number | null
  deviationHours: number | null
  projectedEndDate: string | null
  onTrack: boolean | null
}

/* ------------------------------ 任务变更历史 ------------------------------ */

export interface TaskHistoryEntry {
  recordId?: string
  ts: string
  action: string
  field: string | null
  from: unknown
  to: unknown
  ok: boolean
  error?: string | null
}

export interface TaskHistoryResult {
  planId: string
  taskId: string
  taskName?: string
  entries: TaskHistoryEntry[]
  entryCount: number
}

/* -------------------------------- 交付质量 -------------------------------- */

export interface BlockedTaskRef {
  taskId: string
  taskName?: string
  since: string | null
  blockedHours: number | null
}

export interface QualityResult {
  planId: string
  taskTotal: number
  statusCounts: Record<string, number>
  doneCount: number
  delayedCount: number
  blockedCount: number
  doneRate: number
  delayedRate: number
  onTimeRate: number | null
  onTimeSample: number
  blockedTasks: BlockedTaskRef[]
  totalBlockedHours: number
  engineRiskCount: number
  engineHighRiskCount: number
  registerTotal: number
  registerOpenCount: number
  registerHighOpenCount: number
  issueDensity: number
  plannedEndDate: string | null
}

/* ------------------------------ 配置备份（偏好） ------------------------------ */

export interface PreferenceBlob {
  profile: string
  updatedAt: string | null
  payload: Record<string, unknown> | null
}

/* ------------------------- 知识库（RAG · /api/kb/**） ------------------------- */

/** 检索命中片段（score 为 RRF 融合分或余弦相似度，越大越相关） */
export interface KbHit {
  chunkId?: string | null
  docId?: string | null
  title?: string | null
  source: string
  chunkIndex: number
  tags: string[]
  text: string
  score: number
}

export interface KbHitList {
  query: string
  /** 本次实际使用的检索模式：hybrid（稠密 + BM25 → RRF）/ dense（纯向量） */
  mode: string
  items: KbHit[]
}

export interface KbChunkPreview {
  chunkId?: string | null
  chunkIndex: number
  text: string
}

export interface KbDocument {
  docId: string
  title?: string | null
  source: string
  filename: string
  extension: string
  tags: string[]
  chunkCount: number
  charCount: number
  createdAt: string
}

export interface KbDocumentDetail extends KbDocument {
  chunks: KbChunkPreview[]
}

export interface KbDocumentList {
  items: KbDocument[]
  total: number
}

export interface KbDeleteResult {
  docId: string
  deletedChunks: number
  deleted: boolean
}

export interface KbStats {
  documentCount: number
  chunkCount: number
  totalChars: number
  /** 是否启用混合检索（稠密 + BM25） */
  hybrid: boolean
  /** 当前向量化模型名 */
  embedder: string
  chunkSize: number
  chunkOverlap: number
  topK: number
  maxDocuments: number
  updatedAt: string | null
}

export interface KbDocumentCreatePayload {
  title: string
  content: string
  source?: string
  tags?: string[]
}

export interface KbSearchPayload {
  query: string
  topK?: number
  hybrid?: boolean
}

export interface KbUploadPayload {
  file: File
  title?: string
  source?: string
  /** 标签，逗号分隔（中英文逗号均可） */
  tags?: string
}

/* --------------------- 长期记忆（Mem0 风格 · /api/memories/**） --------------------- */

export const MEMORY_TYPE_VALUES = ['fact', 'preference', 'decision', 'constraint', 'task', 'other'] as const
export type MemoryType = (typeof MEMORY_TYPE_VALUES)[number]

export const MEMORY_IMPORTANCE_VALUES = ['high', 'medium', 'low'] as const
export type MemoryImportance = (typeof MEMORY_IMPORTANCE_VALUES)[number]

export const MEMORY_STATUS_VALUES = ['active', 'disabled'] as const
export type MemoryStatus = (typeof MEMORY_STATUS_VALUES)[number]

export interface MemoryItem {
  memoryId: string
  text: string
  type: string
  importance: string
  tags: string[]
  source: string
  status: string
  createdAt: string
  lastUsedAt: string
  /** 召回时的语义相似度（列表接口恒为 0，检索接口才填充） */
  score: number
  /** 写入是否真正发生（false 表示与既有记忆高度相似被去重跳过） */
  created: boolean
}

export interface MemoryList {
  items: MemoryItem[]
  total: number
}

export interface MemorySearchResult {
  query: string
  items: MemoryItem[]
}

export interface MemoryStats {
  total: number
  active: number
  disabled: number
  byType: Record<string, number>
  maxItems: number
  /** 是否在每轮对话后自动提取 */
  autoExtract: boolean
  topK: number
}

export interface MemoryCreatePayload {
  text: string
  type?: string
  importance?: string
  tags?: string[]
  source?: string
}

export interface MemoryDeleteResult {
  memoryId?: string
  deleted: boolean
}

export interface MemoryClearResult {
  deleted: number
}

/* ---------------------- 访问控制（登录 / 角色 · /api/auth/**） ---------------------- */

/** 角色标识（与后端 app/constants.py 的 ROLES 一致） */
export type AuthRole = 'admin' | 'member' | 'viewer' | 'guest'

/** 权限标识（后端唯一的判定依据；前端仅用于展示收敛，不得据此代替服务端判定） */
export type AuthPermission =
  | 'user:manage'
  | 'plan:read'
  | 'plan:write'
  | 'agent:chat'
  | 'agent:write'
  | 'kb:read'
  | 'kb:write'
  | 'memory:read'
  | 'memory:write'
  | 'audit:read'

/** 计划可见范围：全部计划 / 仅被指派的计划（访客） */
export type PlanScope = 'all' | 'assigned'

/** 当前登录主体（服务端下发，绝不含口令散列等敏感字段） */
export interface AuthUser {
  userId: string
  username: string
  displayName: string
  role: AuthRole
  permissions: AuthPermission[]
  planScope: PlanScope
  /** 访客可见的计划白名单；其余角色为空数组 */
  planIds: string[]
  /** 是否由机器令牌（API_TOKEN）鉴权而来 */
  machine: boolean
}

/** 账号状态 */
export type UserStatus = 'active' | 'disabled'

/** 账号条目（管理员视角） */
export interface AuthAccount {
  userId: string
  username: string
  displayName: string
  role: AuthRole
  status: UserStatus
  createdAt: string
  updatedAt: string
  lastLoginAt: string | null
  /** 访客可见计划白名单；其余角色为空数组 */
  planIds: string[]
}

export interface AuthAccountList {
  items: AuthAccount[]
  total: number
}

/** 角色与权限矩阵条目 */
export interface AuthRoleInfo {
  role: AuthRole
  label: string
  permissions: AuthPermission[]
  planScope: PlanScope
}

/** 新建账号载荷 */
export interface AuthAccountCreatePayload {
  username: string
  password: string
  role: AuthRole
  displayName?: string
}

/** 更新账号载荷（仅提交需要变更的字段） */
export interface AuthAccountUpdatePayload {
  role?: AuthRole
  status?: UserStatus
  displayName?: string
  password?: string
}

/* --------------------------- 异步写作业（/api/jobs/**） --------------------------- */

/** 作业类型（与后端 `app/constants.py` JOB_KINDS 一致） */
export type JobKind = 'plan.create' | 'plan.replan' | 'evals'

/** 作业状态（terminal 取值：succeeded / failed / cancelled） */
export type JobStatus = 'pending' | 'running' | 'succeeded' | 'failed' | 'cancelled'

/** 作业阶段（进度事件里的 phase） */
export type JobPhase = 'queued' | 'engine' | 'settled'

/** 202 受理后返回的作业句柄（调用方据此轮询或订阅进度） */
export interface JobHandle {
  jobId: string
  kind: JobKind
  status: JobStatus
  /** 作业资源地址（GET 快照） */
  pollUrl: string
  /** 进度流地址（SSE） */
  streamUrl: string
  /** 创建时间（Unix 秒，与后端 JobRecord 一致） */
  createdAt: number
}

/** 单条进度项 */
export interface JobProgressItem {
  /** 记录时间（Unix 秒） */
  at: number
  phase: JobPhase | string
  message: string
}

/** 作业快照（GET /api/jobs/{id}）；终态时 result / error 二者之一非空 */
export interface JobSnapshot {
  jobId: string
  kind: JobKind
  status: JobStatus
  planId: string | null
  /** Unix 秒 */
  createdAt: number
  /** Unix 秒 */
  updatedAt: number
  progress: JobProgressItem[]
  /** 成功时的引擎响应信封（`{data: ...}`） */
  result: unknown
  /** 失败时的稳定错误码与通用文案（绝不含内部栈信息） */
  error: { code: string; message: string } | null
}

/* ------------------ 持久化检查点台账（断点续跑 · /api/agent/checkpoints） ------------------ */

/**
 * 运行台账状态机：
 *
 * - `running`            编排进行中（进程重启后可据此重跑）
 * - `awaiting_approval`  已产出待确认动作，等待用户批复（**可确认落库**）
 * - `executed`           待确认动作已全部执行落库
 * - `completed`          重跑完成且无待确认动作（无 LLM 环境下的「跑完即结论」）
 * - `rejected`           用户驳回，待确认动作已被丢弃
 * - `aborted`            主动作废，未执行任何写入
 * - `failed`             编排失败（错误信息保留在 error 字段，可重跑）
 */
export type CheckpointStatus =
  | 'running'
  | 'awaiting_approval'
  | 'executed'
  | 'completed'
  | 'rejected'
  | 'aborted'
  | 'failed'

/** 台账类型：端到端建计划（无会话）或会话式对话 */
export type CheckpointKind = 'plan' | 'chat'

/** 续跑动作：auto = 按当前状态自动分流（待确认 → 确认；运行中 / 失败 → 重跑） */
export type CheckpointAction = 'auto' | 'confirm' | 'retry' | 'abort'

/** 台账列表项（仅摘要，不含请求正文与执行明细） */
export interface CheckpointSummary {
  checkpointId: string
  sessionId: string
  kind: CheckpointKind | string
  status: CheckpointStatus | string
  attempt: number
  planId: string | null
  pendingCount: number
  /** Unix 秒 */
  updatedAt: number
  /** 是否还能续跑（终态台账为 false） */
  resumable: boolean
}

/** 台账列表响应（含后端开关与状态枚举口径，供界面图例对齐） */
export interface CheckpointListResult {
  items: CheckpointSummary[]
  total: number
  enabled: boolean
  /** 存储后端：memory（单副本）/ redis（多副本共享） */
  backend: string
  statuses: string[]
}

/** 台账快照（含请求原文、待确认动作、执行结果与反思轨迹） */
export interface CheckpointDetail {
  checkpointId: string
  sessionId: string
  owner: string
  kind: CheckpointKind | string
  status: CheckpointStatus | string
  attempt: number
  /** 保存的**原始请求**：重跑时据它重建整轮编排，用户无需重新描述需求 */
  request: {
    kind?: string
    message: string
    planId?: string | null
    deferWrites?: boolean
    iterationDeadline?: string | null
    iterationStartDate?: string | null
    devMemberCount?: number | null
  }
  pendingActions: AgentPendingAction[]
  executed: AgentToolCallRecord[]
  content: string
  planId: string | null
  reflections: AgentReflection[]
  error: string | null
  /** Unix 秒 */
  createdAt: number
  /** Unix 秒 */
  updatedAt: number
  resumable: boolean
}

/** 续跑结果 */
export interface CheckpointResumeResult {
  checkpointId: string
  replayed: boolean
  executed: AgentToolCallRecord[]
  remainingActions: AgentPendingAction[]
  checkpoint: CheckpointDetail
  rejected: boolean
}

/* --------------------- Agent 评测台（黄金集 + 双轨打分 + CI 门禁） --------------------- */

/** 用例类型：plan（端到端建计划）/ chat（会话任务）/ reflection（确定性自愈演练，无需 LLM） */
export type EvalCaseKind = 'plan' | 'chat' | 'reflection'

/** 单例产出 */
export type EvalOutcome = 'pass' | 'fail' | 'error'

/** 黄金集用例声明（界面据此说明「到底在评什么」） */
export interface EvalCaseSpec {
  id: string
  kind: EvalCaseKind | string
  title: string
  /** 该用例依赖的前置用例（其产出的计划 ID 会注入本用例） */
  dependsOn?: string | null
  /** 是否需要回收自建的临时计划 */
  cleanup?: boolean
  /** 确定性演练剧本名 */
  drill?: string | null
  input: Record<string, unknown>
  /** 期望断言（键为断言名，值为期望条件） */
  expect: Record<string, unknown>
  rubric?: string | null
}

/** 单条断言的执行结果 */
export interface EvalAssertionResult {
  name: string
  ok: boolean
  detail: string
  /** 该断言是否为硬门槛（不过即判例失败） */
  hard: boolean
}

/** 单例评测结果 */
export interface EvalCaseResult {
  caseId: string
  kind: EvalCaseKind | string
  title: string
  outcome: EvalOutcome | string
  assertions: EvalAssertionResult[]
  /** 确定性断言得分（0~1，只算硬门槛） */
  deterministicScore: number
  /** LLM 评委得分；评委缺席（未配置 / 输出不可解析）为 null，而不是 0 */
  judgeScore: number | null
  judgeLevel: string | null
  judgeRationale: string | null
  score: number
  toolChain: string[]
  steps: number
  pendingCount: number
  planId: string | null
  content: string
  durationMs: number
  error: string | null
  /** 本例的执行轨迹 ID：评测只说「挂了」，要知「为什么挂」需据此跳回放 */
  traceId?: string | null
}

/** 门禁结论（CI 以此判定放行） */
export interface EvalGateResult {
  passed: boolean
  /** 未通过原因（人类可读，逐条列出） */
  reasons: string[]
  passRate: number
  avgScore: number
  thresholds: { passRate: number; minScore: number }
}

/** 一次评测运行的完整结果 */
export interface EvalSuiteResult {
  runId: string
  startedAt: string
  durationMs: number
  total: number
  passed: number
  failed: number
  errored: number
  passRate: number
  avgScore: number
  cases: EvalCaseResult[]
  gate: EvalGateResult | null
  /** 运行说明（依赖被过滤、LLM 未配置、临时计划回收失败等） */
  notes: string[]
  model: string | null
  backend: string
}

/** 评测台自述（黄金集清单 / 门禁阈值 / 评委开关 / 当前模型） */
export interface EvalDescribeResult {
  enabled: boolean
  caseCount: number
  cases: EvalCaseSpec[]
  /** 已沉淀的自定义（回归）用例数：它们会自动并入运行集与 CI 门禁 */
  customCaseCount?: number
  customCases?: EvalCaseSpec[]
  judgeEnabled: boolean
  gates: { passRate: number; minScore: number }
  model: string | null
  backend: string
  jobBackend: string
  /** 执行轨迹存储后端（memory / redis） */
  traceBackend?: string
  /** 自定义用例存储后端（memory / redis） */
  customCaseBackend?: string
}

/** 触发评测运行的请求 */
export interface EvalRunRequest {
  /** 只跑指定用例 id */
  caseIds?: string[]
  /** 按类型过滤：plan / chat / reflection */
  kinds?: EvalCaseKind[]
  /** 是否启用 LLM 评委（缺省按服务端配置） */
  judge?: boolean
  /**
   * 是否转成异步作业（返回 JobHandle）。
   *
   * 评测会真实驱动 Agent 跑多轮 LLM + 引擎调用，很容易超过中间层超时；
   * 长耗时场景应置 true 并改用 `jobStream` 订阅进度。
   */
  enqueue?: boolean
}

/* ---------------- Agent 执行追踪与回放（/api/agent/traces/**） ---------------- */

/** 轨迹时间片类型：模型调用 / 工具调用 / 反思 / 护栏 / 上下文 / 收尾 */
export type TraceSpanKind = 'llm' | 'tool' | 'reflect' | 'guard' | 'context' | 'final'

/** 时间片状态 */
export type TraceSpanStatus = 'ok' | 'error' | 'deferred' | 'degraded'

/** 一次运行的终态 */
export type TraceRunStatus = 'ok' | 'pending' | 'degraded' | 'error'

/** 轨迹时间片（input / output 为摘要，不是全量载荷） */
export interface TraceSpan {
  index: number
  kind: TraceSpanKind | string
  title: string
  /** 相对轨迹起点的时间偏移（秒），用于瀑布图定位 */
  at: number
  durationMs: number
  status: TraceSpanStatus | string
  /** 失败 / 降级时的稳定错误码 */
  code?: string | null
  /** 所属编排步号（0 表示步外，如收尾） */
  step: number
  input: Record<string, unknown>
  output: Record<string, unknown>
  detail: string
}

/** 轨迹列表项（不含 span 明细） */
export interface TraceSummary {
  traceId: string
  sessionId: string
  kind: 'plan' | 'chat' | string
  status: TraceRunStatus | string
  steps: number
  planId: string | null
  pendingCount: number
  spanCount: number
  toolChain: string[]
  failedTools: string[]
  reflectionCount: number
  /** 一致性护栏触发次数（谎言完成 / 无依据作答 / 空转被拦） */
  guardCount: number
  durationMs: number
  /** Unix 秒 */
  createdAt: number
  /** 是否含事故信号（由服务端确定性计算，前端不自行推断） */
  hasIncident: boolean
  /** 已沉淀的回归用例 ID（非空则隐藏重复沉淀入口） */
  incidentCaseId?: string | null
  contentPreview: string
}

export interface TraceListResult {
  items: TraceSummary[]
  total: number
  enabled: boolean
  backend: string
  kinds: string[]
  statuses: string[]
}

/** 轨迹全量（span 时序 + 原始请求） */
export interface TraceDetail {
  traceId: string
  sessionId: string
  owner: string
  kind: 'plan' | 'chat' | string
  status: TraceRunStatus | string
  /** 原始请求：回放与沉淀回归用例都凭它原样重跑 */
  request: {
    kind?: string
    message: string
    planId?: string | null
    deferWrites?: boolean
    iterationDeadline?: string | null
    iterationStartDate?: string | null
    devMemberCount?: number | null
  } | null
  spans: TraceSpan[]
  steps: number
  planId: string | null
  pendingCount: number
  toolChain: string[]
  failedTools: string[]
  reflectionCount: number
  guardCount: number
  contentPreview: string
  error: string | null
  durationMs: number
  createdAt: number
  hasIncident: boolean
  incidentCaseId?: string | null
}

/** 回放参数（稳定契约：与 span 明细字段增减解耦） */
export interface TraceReplay {
  kind: 'plan' | 'chat' | string
  /** 原始请求入参（agent/traces 只回放 chat 或 plan 的输入） */
  input: Record<string, unknown>
  traceId: string
  status: TraceRunStatus | string
  error?: string | null
  runStatuses: string[]
  hasIncident: boolean
  incidentCaseId?: string | null
}

/* ------------------- 事故 → 回归用例闭环（/api/evals/cases） ------------------- */

/** 自定义用例来源：由失败轨迹推导 / 人工登记 */
export type CustomCaseOrigin = 'incident' | 'manual'

/** 持久化的自定义（回归）用例 */
export interface CustomEvalCase {
  case: EvalCaseSpec
  origin: CustomCaseOrigin | string
  originTraceId?: string | null
  note: string
  owner: string
  /** Unix 秒 */
  createdAt: number
  /** 断言推导依据（每条断言为什么被选中，必须可复核） */
  rationale: string[]
  /** 是否已自动纳入运行集与门禁 */
  included: boolean
}

export interface CustomCaseListResult {
  items: CustomEvalCase[]
  total: number
  enabled: boolean
  backend: string
  origins: string[]
  maxCases: number
}

/** 生效用例集（黄金集 + 自定义），用于核对「门禁到底覆盖了哪些用例」 */
export interface EffectiveCasesResult {
  total: number
  cases: EvalCaseSpec[]
  customIds: string[]
}

/** 沉淀回归用例的返回（含该轨迹与沉淀后的生效用例总数） */
export interface IncidentCaseResult extends CustomEvalCase {
  traceId: string
  effectiveCaseCount: number
}

/* -------------------- 输出溯源与置信度标注（P5 · 可点开引用） -------------------- */

/** 证据来源：工具返回字段 / 知识库片段 / 长期记忆 */
export type ProvenanceSource = 'tool' | 'kb' | 'memory'

/** 声明状态：数值可回溯 / 找不到来源 */
export type ProvenanceStatus = 'verified' | 'unverified'

/** 一条证据引用 */
export interface ProvenanceCitation {
  source: ProvenanceSource | string
  /** 可定位引用：工具名.字段路径 / 知识库 chunkId / 记忆 ID */
  ref: string
  label: string
  value: string
}

/** 结论中的一条事实性声明及其核实状态 */
export interface ClaimCitation {
  index: number
  text: string
  status: ProvenanceStatus | string
  /** 该声明中出现的数字字面量 */
  numbers: string[]
  citations: ProvenanceCitation[]
  detail: string
}

/** 一次运行的溯源报告 */
export interface ProvenanceReport {
  totalClaims: number
  verified: number
  /** 未匹配到任何证据的数字声明数（诚实暴露不确定） */
  unverified: number
  /** 已核实声明占比（无声明时为 1.0） */
  confidence: number
  evidenceCount: number
  sources: Record<string, number>
  claims: ClaimCitation[]
}

/* --------------------- 动态上下文预算管理（P6 · 分区预算） --------------------- */

/** 上下文分区：系统提示 / 必保工作区 / 本轮输入 / 检索增强 / 历史回合 */
export type BudgetZone = 'system' | 'keep' | 'current' | 'recall' | 'history'

export interface BudgetZoneUsage {
  zone: BudgetZone | string
  label: string
  chars: number
  messages: number
  /** 该分区被裁掉的消息条数 */
  trimmed: number
  /** 该分区是否被压缩（历史回合折叠为一行摘要） */
  compressed: boolean
}

/** 一次编排的分区预算明细 */
export interface BudgetReport {
  limit: number
  used: number
  /** 用量占预算比例（0~1） */
  utilization: number
  overBudget: boolean
  zones: BudgetZoneUsage[]
  /** 本轮上下文被改写 / 截断的说明（让「为什么被裁」可解释） */
  notes: string[]
}
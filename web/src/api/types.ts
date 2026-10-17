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

/** WebSocket 推送事件 */
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
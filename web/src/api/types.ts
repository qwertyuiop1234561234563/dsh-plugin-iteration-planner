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
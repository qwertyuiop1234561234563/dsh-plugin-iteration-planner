/**
 * 展示层映射与格式化工具。
 *
 * 所有枚举 → 文案 / 颜色的映射集中在此，避免散落在各组件中（与后端「枚举统一管理」的规范对齐）。
 */

import type {
  IssueProbability,
  IssueSeverity,
  IssueStatus,
  Priority,
  RiskLevel,
  TaskStatus,
  TaskType,
  UpdatableField,
} from '@/api/types'

/** Element Plus 标签样式类型 */
export type TagType = 'primary' | 'success' | 'info' | 'warning' | 'danger'

/** 任务类型文案 */
export const TASK_TYPE_LABEL: Record<TaskType, string> = {
  DB_DESIGN: '数据库设计',
  API_DEV: '接口开发',
  BUSINESS_CODE: '业务开发',
  DEBUG: '联调自测',
  TEST: '测试',
  DEPLOY: '部署发布',
  DOC: '文档',
}

/** 任务类型配色（用于 DAG 画布与标签；取自蓝晒制图台色系） */
export const TASK_TYPE_COLOR: Record<TaskType, string> = {
  DB_DESIGN: '#8b5cf6',
  API_DEV: '#0e6ba8',
  BUSINESS_CODE: '#2f9e6b',
  DEBUG: '#e08b1a',
  TEST: '#17a2b8',
  DEPLOY: '#d64545',
  DOC: '#6b8296',
}

/** 优先级配色（仪表盘图表用） */
export const PRIORITY_COLOR: Record<Priority, string> = {
  P0: '#d64545',
  P1: '#e08b1a',
  P2: '#0e6ba8',
  P3: '#98a2b3',
}

/** 风险等级配色（仪表盘 / 报告用） */
export const RISK_COLOR: Record<RiskLevel, string> = {
  HIGH: '#d64545',
  MEDIUM: '#e08b1a',
  LOW: '#6b8296',
}

/** 任务状态文案 */
export const STATUS_LABEL: Record<TaskStatus, string> = {
  TODO: '待办',
  IN_PROGRESS: '进行中',
  BLOCKED: '阻塞',
  TESTING: '测试中',
  DONE: '已完成',
  DELAYED: '已延期',
}

/** 任务状态标签样式 */
export const STATUS_TAG: Record<TaskStatus, TagType> = {
  TODO: 'info',
  IN_PROGRESS: 'primary',
  BLOCKED: 'danger',
  TESTING: 'warning',
  DONE: 'success',
  DELAYED: 'danger',
}

/** 任务状态色（累积流图 / 仪表盘堆叠图，与标签样式同源） */
export const STATUS_COLOR: Record<TaskStatus, string> = {
  TODO: '#b3c9d8',
  IN_PROGRESS: '#0e6ba8',
  BLOCKED: '#d64545',
  TESTING: '#e08b1a',
  DONE: '#2f9e6b',
  DELAYED: '#8b5cf6',
}

/**
 * 累积流图工作流顺序（自下而上堆叠）：
 * 已完成为底、待办为顶，顶部带宽随进度收窄，符合累积流图的常规划读法。
 */
export const STATUS_FLOW_ORDER: TaskStatus[] = ['DONE', 'TESTING', 'IN_PROGRESS', 'BLOCKED', 'DELAYED', 'TODO']

/** 优先级标签样式 */
export const PRIORITY_TAG: Record<Priority, TagType> = {
  P0: 'danger',
  P1: 'warning',
  P2: 'primary',
  P3: 'info',
}

/** 风险等级文案 */
export const RISK_LABEL: Record<RiskLevel, string> = {
  HIGH: '高',
  MEDIUM: '中',
  LOW: '低',
}

/** 风险等级标签样式 */
export const RISK_TAG: Record<RiskLevel, TagType> = {
  HIGH: 'danger',
  MEDIUM: 'warning',
  LOW: 'info',
}

/** 时间戳 → 本地时间字符串 */
export function formatTime(timestamp: number): string {
  if (!timestamp) return '—'
  return new Date(timestamp).toLocaleString('zh-CN', { hour12: false })
}

/** 时间戳 → 相对时间（通知中心 / 时间线用） */
export function relativeTime(timestamp: number, now = Date.now()): string {
  const diff = now - timestamp
  if (diff < 0) return '刚刚'
  const minute = 60_000
  const hour = 60 * minute
  const day = 24 * hour
  if (diff < minute) return '刚刚'
  if (diff < hour) return `${Math.floor(diff / minute)} 分钟前`
  if (diff < day) return `${Math.floor(diff / hour)} 小时前`
  if (diff < 30 * day) return `${Math.floor(diff / day)} 天前`
  return new Date(timestamp).toLocaleDateString('zh-CN')
}

/** ISO 字符串 → 本地时间字符串 */
export function formatIso(iso: string): string {
  if (!iso) return '—'
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString('zh-CN', { hour12: false })
}

/** 工时 → 工作日描述（单日 8 小时） */
export function hoursToWorkDays(hours: number): string {
  return `${Math.max(1, Math.ceil(hours / 8))} 个工作日`
}

/** 需求文本截断，用于列表展示 */
export function truncate(text: string, max = 48): string {
  return text.length > max ? `${text.slice(0, max)}…` : text
}

/* ------------------------------ 治理层枚举映射 ------------------------------ */

/** 审计动作 → 可读文案（未知动作回退为原始 action；时间线 / 变更历史共用） */
export const AUDIT_ACTION_LABEL: Record<string, string> = {
  plan_iteration: '完整规划流水线',
  task_decompose: '需求拆解',
  'task_decompose.degrade': '需求拆解降级（规则兜底）',
  replan_all: '全量重规划',
  task_add: '新增任务',
  task_update: '修改任务',
  task_delete: '删除任务',
  risk_scan: '风险扫描',
  'risk_scan.degrade': '风险扫描降级（规则兜底）',
  'dag_check.dangling': '悬空依赖提示',
  report_generate: '生成报表',
  'api.startup': '服务启动',
}

/** 审计动作文案（集中映射，避免各页面重复维护） */
export function auditActionLabel(action: string): string {
  return AUDIT_ACTION_LABEL[action] ?? action
}

/**
 * Agent 工具名 → 可读文案。
 *
 * 与后端 `python/app/agent/schemas.py` 的 `TOOL_ACTION_LABELS` 保持同一口径，
 * 供工具调用记录（dot 过程展示）与待确认动作卡片使用。
 */
export const AGENT_TOOL_LABEL: Record<string, string> = {
  plan_iteration: '生成新计划',
  replan_all: '全量重规划',
  get_plan: '读取计划',
  list_plans: '列出计划',
  task_add: '新增任务',
  task_update: '修改任务',
  task_delete: '删除任务',
  dag_build: '构建 DAG 视图',
  dag_check: '拓扑校验',
  schedule_calc: '关键路径计算',
  risk_scan: '风险扫描',
  report_generate: '生成报表',
}

/** Agent 工具文案（未知工具回退为原始工具名） */
export function agentToolLabel(tool: string): string {
  return AGENT_TOOL_LABEL[tool] ?? tool
}

/** 任务可更新字段 → 文案（变更历史 / 批量编辑共用） */
export const UPDATABLE_FIELD_LABEL: Record<UpdatableField, string> = {
  estimatedHours: '预估工时',
  predecessorTaskIds: '前置任务',
  status: '状态',
  priority: '优先级',
  assignee: '责任人',
}

/** 人工风险严重度文案 */
export const ISSUE_SEVERITY_LABEL: Record<IssueSeverity, string> = {
  LOW: '低',
  MEDIUM: '中',
  HIGH: '高',
  CRITICAL: '严重',
}

/** 人工风险严重度标签样式 */
export const ISSUE_SEVERITY_TAG: Record<IssueSeverity, TagType> = {
  LOW: 'info',
  MEDIUM: 'warning',
  HIGH: 'danger',
  CRITICAL: 'danger',
}

/** 人工风险严重度配色（质量看板图表用） */
export const ISSUE_SEVERITY_COLOR: Record<IssueSeverity, string> = {
  LOW: '#6b8296',
  MEDIUM: '#e08b1a',
  HIGH: '#d64545',
  CRITICAL: '#8b5cf6',
}

/** 人工风险发生概率文案 */
export const ISSUE_PROBABILITY_LABEL: Record<IssueProbability, string> = {
  LOW: '低',
  MEDIUM: '中',
  HIGH: '高',
}

/** 人工风险处置状态文案 */
export const ISSUE_STATUS_LABEL: Record<IssueStatus, string> = {
  OPEN: '待处理',
  MITIGATING: '缓解中',
  MONITORING: '监控中',
  CLOSED: '已关闭',
  ACCEPTED: '已接受',
}

/** 人工风险处置状态标签样式 */
export const ISSUE_STATUS_TAG: Record<IssueStatus, TagType> = {
  OPEN: 'danger',
  MITIGATING: 'warning',
  MONITORING: 'primary',
  CLOSED: 'success',
  ACCEPTED: 'info',
}
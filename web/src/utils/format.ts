/**
 * 展示层映射与格式化工具。
 *
 * 所有枚举 → 文案 / 颜色的映射集中在此，避免散落在各组件中（与后端「枚举统一管理」的规范对齐）。
 */

import type { Priority, RiskLevel, TaskStatus, TaskType } from '@/api/types'

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
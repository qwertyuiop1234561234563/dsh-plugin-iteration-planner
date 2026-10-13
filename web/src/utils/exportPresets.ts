/**
 * 导出预设：集中定义各数据集的导出列与文件命名。
 *
 * 统一放在这里的原因：任务导出既可由「任务管理」页的按钮触发，也可由全局快捷键
 * （Ctrl+Shift+E）触发，两处必须给出同一份列定义，避免口径漂移。
 */

import type { AuditRecord, PlanSummary, ProjectPlan, Task } from '@/api/types'
import type { ExportColumn } from '@/utils/export'
import { RISK_LABEL, STATUS_LABEL, TASK_TYPE_LABEL, formatTime } from '@/utils/format'

/** 迭代概览（计划列表）导出列 */
export const PLAN_EXPORT_COLUMNS: Array<ExportColumn<PlanSummary>> = [
  { label: '计划ID', value: (row) => row.planId },
  { label: '原始需求', value: (row) => row.rawRequirement },
  { label: '迭代起始', value: (row) => row.iterationStartDate },
  { label: '迭代截止', value: (row) => row.iterationDeadline },
  { label: '开发人数', value: (row) => row.devMemberCount },
  { label: '任务数', value: (row) => row.taskCount },
  { label: '总工时(h)', value: (row) => row.totalEstimatedHours },
  { label: '关键路径工期(h)', value: (row) => row.totalDurationHours },
  { label: '风险总数', value: (row) => row.riskCount },
  { label: '高风险数', value: (row) => row.highRiskCount },
  { label: '创建时间', value: (row) => formatTime(row.createdAt) },
  { label: '更新时间', value: (row) => formatTime(row.updatedAt) },
]

/**
 * 任务导出列。
 *
 * 关键路径 / 里程碑标记由调用方以「只读上下文」传入，不在此处重复判定逻辑。
 */
export function taskExportColumns(context: {
  criticalIds: Set<string>
  milestoneIds: Set<string>
  riskyIds: Set<string>
}): Array<ExportColumn<Task>> {
  return [
    { label: '任务ID', value: (row) => row.taskId },
    { label: '任务名称', value: (row) => row.taskName },
    { label: '任务描述', value: (row) => row.taskDesc },
    { label: '类型', value: (row) => TASK_TYPE_LABEL[row.taskType] ?? row.taskType },
    { label: '状态', value: (row) => STATUS_LABEL[row.status] ?? row.status },
    { label: '优先级', value: (row) => row.priority },
    { label: '责任人', value: (row) => row.assignee ?? '待分配' },
    { label: '预估工时(h)', value: (row) => row.estimatedHours },
    { label: '最早开始(h)', value: (row) => row.es },
    { label: '最晚开始(h)', value: (row) => row.ls },
    { label: '松弛时间(h)', value: (row) => row.slack },
    { label: '关键路径', value: (row) => (context.criticalIds.has(row.taskId) ? '是' : '否') },
    { label: '里程碑', value: (row) => (context.milestoneIds.has(row.taskId) ? '是' : '否') },
    { label: '命中风险', value: (row) => (context.riskyIds.has(row.taskId) ? '是' : '否') },
    { label: '前置任务数', value: (row) => row.predecessorTaskIds.length },
    { label: '前置任务', value: (row) => row.predecessorTaskIds.join(' / ') },
  ]
}

/** 风险清单导出列 */
export const RISK_EXPORT_COLUMNS: Array<ExportColumn<ProjectPlan['riskList'][number]>> = [
  { label: '风险ID', value: (row) => row.riskId },
  { label: '等级', value: (row) => RISK_LABEL[row.level] ?? row.level },
  { label: '关联任务数', value: (row) => row.taskIds.length },
  { label: '关联任务', value: (row) => row.taskIds.join(' / ') },
  { label: '风险描述', value: (row) => row.description },
  { label: '缓解建议', value: (row) => row.suggest },
]

/** 审计日志导出列 */
export const AUDIT_EXPORT_COLUMNS: Array<ExportColumn<AuditRecord>> = [
  { label: '记录ID', value: (row) => row.recordId },
  { label: '计划ID', value: (row) => row.planId ?? '' },
  { label: '时间', value: (row) => formatTime(Date.parse(row.ts)) },
  { label: '动作', value: (row) => row.action },
  { label: '结果', value: (row) => (row.ok ? '成功' : '失败') },
  { label: '耗时(ms)', value: (row) => row.durationMs },
  { label: '入参', value: (row) => (row.input ? JSON.stringify(row.input) : '') },
  { label: '出参', value: (row) => (typeof row.output === 'string' ? row.output : row.output ? JSON.stringify(row.output) : '') },
  { label: '错误', value: (row) => row.error ?? '' },
]

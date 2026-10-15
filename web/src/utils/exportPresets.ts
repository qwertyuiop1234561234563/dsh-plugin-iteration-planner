/**
 * 导出预设：集中定义各数据集的导出列与文件命名。
 *
 * 统一放在这里的原因：任务导出既可由「任务管理」页的按钮触发，也可由全局快捷键
 * （Ctrl+Shift+E）触发，两处必须给出同一份列定义，避免口径漂移。
 */

import type {
  AuditRecord,
  BaselineDiff,
  BaselineSummary,
  BlockedTaskRef,
  PlanSummary,
  ProjectPlan,
  RegisterItem,
  Task,
  UnassignedTaskRef,
  WorkloadMember,
} from '@/api/types'
import type { ExportColumn } from '@/utils/export'
import {
  ISSUE_PROBABILITY_LABEL,
  ISSUE_SEVERITY_LABEL,
  ISSUE_STATUS_LABEL,
  RISK_LABEL,
  STATUS_LABEL,
  TASK_TYPE_LABEL,
  UPDATABLE_FIELD_LABEL,
  formatTime,
} from '@/utils/format'

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

/* ------------------------------ 治理数据导出列 ------------------------------ */

/** 基线快照列表导出列 */
export const BASELINE_EXPORT_COLUMNS: Array<ExportColumn<BaselineSummary>> = [
  { label: '基线ID', value: (row) => row.baselineId },
  { label: '名称', value: (row) => row.name },
  { label: '备注', value: (row) => row.note ?? '' },
  { label: '创建时间', value: (row) => formatTime(Date.parse(row.createdAt)) },
  { label: '任务数', value: (row) => row.taskCount },
  { label: '关键路径任务数', value: (row) => row.criticalTaskCount },
  { label: '风险总数', value: (row) => row.riskCount },
  { label: '高风险数', value: (row) => row.highRiskCount },
  { label: '总工时(h)', value: (row) => row.totalEstimatedHours },
  { label: '关键工期(h)', value: (row) => row.totalDurationHours },
  { label: '计划完成日', value: (row) => row.plannedEndDate ?? '' },
]

/** 基线偏差对比的扁平化导出行 */
export interface BaselineDiffExportRow {
  category: string
  taskId: string
  taskName: string
  field: string
  baselineValue: string
  currentValue: string
  delta: string
}

/** 把基线偏差对比结果摊平为可导出的行（纯格式化，不改变任何数值） */
export function flattenBaselineDiff(diff: BaselineDiff, metricLabels: Record<string, string>): BaselineDiffExportRow[] {
  const rows: BaselineDiffExportRow[] = []
  for (const [key, item] of Object.entries(diff.metrics)) {
    rows.push({
      category: '指标',
      taskId: '',
      taskName: metricLabels[key] ?? key,
      field: metricLabels[key] ?? key,
      baselineValue: item.from === null || item.from === undefined ? '' : String(item.from),
      currentValue: item.to === null || item.to === undefined ? '' : String(item.to),
      delta: item.delta === null || item.delta === undefined ? '' : String(item.delta),
    })
  }
  for (const task of diff.added) {
    rows.push({
      category: '新增任务',
      taskId: task.taskId,
      taskName: task.taskName ?? '',
      field: '',
      baselineValue: '',
      currentValue: `${task.estimatedHours ?? 0}h`,
      delta: '',
    })
  }
  for (const task of diff.removed) {
    rows.push({
      category: '删除任务',
      taskId: task.taskId,
      taskName: task.taskName ?? '',
      field: '',
      baselineValue: `${task.estimatedHours ?? 0}h`,
      currentValue: '',
      delta: '',
    })
  }
  for (const task of diff.changed) {
    for (const field of task.fields) {
      rows.push({
        category: '字段变更',
        taskId: task.taskId,
        taskName: task.taskName ?? '',
        field: UPDATABLE_FIELD_LABEL[field.field as keyof typeof UPDATABLE_FIELD_LABEL] ?? field.field,
        baselineValue: String(field.from ?? '—'),
        currentValue: String(field.to ?? '—'),
        delta: '',
      })
    }
  }
  return rows
}

/** 基线偏差对比导出列 */
export const BASELINE_DIFF_EXPORT_COLUMNS: Array<ExportColumn<BaselineDiffExportRow>> = [
  { label: '类别', value: (row) => row.category },
  { label: '任务ID', value: (row) => row.taskId },
  { label: '任务/指标', value: (row) => row.taskName },
  { label: '字段', value: (row) => row.field },
  { label: '基线值', value: (row) => row.baselineValue },
  { label: '现状值', value: (row) => row.currentValue },
  { label: '偏差', value: (row) => row.delta },
]

/** 人工风险登记册导出列 */
export const REGISTER_EXPORT_COLUMNS: Array<ExportColumn<RegisterItem>> = [
  { label: '条目ID', value: (row) => row.issueId },
  { label: '标题', value: (row) => row.title },
  { label: '严重度', value: (row) => ISSUE_SEVERITY_LABEL[row.severity] ?? row.severity },
  { label: '发生概率', value: (row) => ISSUE_PROBABILITY_LABEL[row.probability] ?? row.probability },
  { label: '处置状态', value: (row) => ISSUE_STATUS_LABEL[row.status] ?? row.status },
  { label: '责任人', value: (row) => row.owner ?? '' },
  { label: '关联任务数', value: (row) => row.linkedTaskIds.length },
  { label: '关联任务', value: (row) => row.linkedTaskIds.join(' / ') },
  { label: '缓解措施', value: (row) => row.mitigation ?? '' },
  { label: '描述', value: (row) => row.description ?? '' },
  { label: '时限', value: (row) => row.dueDate ?? '' },
  { label: '创建时间', value: (row) => formatTime(Date.parse(row.createdAt)) },
  { label: '更新时间', value: (row) => formatTime(Date.parse(row.updatedAt)) },
]

/** 人员负荷（成员维度）导出列 */
export const WORKLOAD_EXPORT_COLUMNS: Array<ExportColumn<WorkloadMember>> = [
  { label: '责任人', value: (row) => row.assignee },
  { label: '任务数', value: (row) => row.taskCount },
  { label: '关键路径任务数', value: (row) => row.criticalCount },
  { label: '预估工时(h)', value: (row) => row.estimatedHours },
  { label: '剩余工时(h)', value: (row) => row.remainingHours },
  { label: '可用容量(h)', value: (row) => row.capacityHours },
  { label: '负荷率', value: (row) => `${Math.round(row.loadRatio * 100)}%` },
  { label: '是否超载', value: (row) => (row.overloaded ? '是' : '否') },
]

/** 待分配任务池导出列 */
export const UNASSIGNED_EXPORT_COLUMNS: Array<ExportColumn<UnassignedTaskRef>> = [
  { label: '任务ID', value: (row) => row.taskId },
  { label: '任务名称', value: (row) => row.taskName },
  { label: '预估工时(h)', value: (row) => row.estimatedHours },
  { label: '优先级', value: (row) => row.priority ?? '' },
  { label: '关键路径', value: (row) => (row.critical ? '是' : '否') },
]

/** 交付质量：阻塞任务导出列 */
export const BLOCKED_EXPORT_COLUMNS: Array<ExportColumn<BlockedTaskRef>> = [
  { label: '任务ID', value: (row) => row.taskId },
  { label: '任务名称', value: (row) => row.taskName ?? '' },
  { label: '进入阻塞时间', value: (row) => (row.since ? formatTime(Date.parse(row.since)) : '') },
  { label: '阻塞时长(h)', value: (row) => (row.blockedHours === null ? '' : row.blockedHours) },
]

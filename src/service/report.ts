/**
 * 迭代报表生成（§四.10）：输出「团队可读文本报告」+「燃尽图原始数据」。
 *
 * 报告结构固定为：概览 → 关键路径 → 任务清单 → 风险清单 → 燃尽图数据。
 * 高风险项在文本中以【HIGH】高亮，便于研发团队快速定位。
 */

import { RiskLevel, SCHEDULE, TaskStatus } from '../constants/enums.js'
import type { BurndownItem, ProjectPlan, Task } from '../models/entities.js'

export interface ReportResult {
  /** 适合研发团队阅读的文本报告 */
  text: string
  /** 燃尽图原始数据（同一份数据同时随 ProjectPlan 返回） */
  burndownRawData: BurndownItem[]
}

/** 任务状态 → 展示标签 */
const STATUS_LABEL: Record<TaskStatus, string> = {
  [TaskStatus.TODO]: '待办',
  [TaskStatus.IN_PROGRESS]: '进行中',
  [TaskStatus.BLOCKED]: '阻塞',
  [TaskStatus.TESTING]: '测试中',
  [TaskStatus.DONE]: '已完成',
  [TaskStatus.DELAYED]: '已延期',
}

/** 渲染单个任务的一行清单 */
function renderTaskRow(task: Task, index: number, nameOf: (id: string) => string): string {
  const preds = task.predecessorTaskIds.length > 0 ? task.predecessorTaskIds.map(nameOf).join('、') : '—'
  return [
    `${index + 1}`,
    task.taskName,
    task.taskType,
    task.priority,
    `${task.estimatedHours}h`,
    task.assignee ?? '待分配',
    preds,
    STATUS_LABEL[task.status] ?? task.status,
    `es=${task.es}`,
    `ls=${task.ls}`,
    `slack=${task.slack}`,
  ].join(' | ')
}

/** 生成完整迭代报表 */
export function buildReport(plan: ProjectPlan): ReportResult {
  const nameOf = (id: string): string => plan.taskList.find((t) => t.taskId === id)?.taskName ?? id
  const workDays = Math.max(1, Math.ceil(plan.totalDurationHours / SCHEDULE.WORK_HOURS_PER_DAY))

  const lines: string[] = []
  lines.push('# 迭代规划报告')
  lines.push('')
  lines.push(`- 计划 ID：${plan.planId}`)
  lines.push(`- 迭代窗口：${plan.iterationStartDate} ~ ${plan.iterationDeadline}`)
  lines.push(`- 开发人数：${plan.devMemberCount}`)
  lines.push(`- 原始需求：${plan.rawRequirement}`)
  lines.push('')

  lines.push('## 一、概览')
  lines.push('')
  lines.push(`- 任务总数：${plan.taskList.length}`)
  lines.push(`- 项目总预估工时：${plan.totalEstimatedHours}h`)
  lines.push(`- 关键路径总工期：${plan.totalDurationHours}h（约 ${workDays} 个工作日）`)
  lines.push(`- 风险条目：${plan.riskList.length}（HIGH ${plan.riskList.filter((r) => r.level === RiskLevel.HIGH).length}）`)
  lines.push('')

  lines.push('## 二、关键路径')
  lines.push('')
  if (plan.criticalPath.length === 0) {
    lines.push('（无关键路径，可能任务集为空）')
  } else {
    plan.criticalPath.forEach((id, i) => {
      const task = plan.taskList.find((t) => t.taskId === id)
      lines.push(`${i + 1}. ${task ? `${task.taskName}（${task.taskType}，${task.estimatedHours}h）` : id}`)
    })
  }
  lines.push('')

  lines.push('## 三、任务清单')
  lines.push('')
  lines.push('序号 | 任务 | 类型 | 优先级 | 工时 | 负责人 | 前置 | 状态 | 时间窗')
  lines.push('---|---|---|---|---|---|---|---|---')
  plan.taskList.forEach((task, i) => lines.push(renderTaskRow(task, i, nameOf)))
  lines.push('')

  lines.push('## 四、风险清单')
  lines.push('')
  if (plan.riskList.length === 0) {
    lines.push('未检测到风险。')
  } else {
    for (const risk of plan.riskList) {
      // 高等级风险加粗高亮
      const head = risk.level === RiskLevel.HIGH ? `- **【${risk.level}】**` : `- 【${risk.level}】`
      lines.push(`${head} ${risk.description}`)
      lines.push(`  - 关联任务：${risk.taskIds.length > 0 ? risk.taskIds.map(nameOf).join('、') : '—'}`)
      lines.push(`  - 整改建议：${risk.suggest}`)
    }
  }
  lines.push('')

  lines.push('## 五、燃尽图原始数据')
  lines.push('')
  lines.push('日期 | 剩余工时')
  lines.push('---|---')
  for (const item of plan.burndownRawData) {
    lines.push(`${item.time} | ${item.remainingHours}h`)
  }

  return { text: lines.join('\n'), burndownRawData: plan.burndownRawData }
}
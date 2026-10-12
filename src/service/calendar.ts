/**
 * 排期日历与人员负载业务模块（§3.3.2 / §4.2）。
 *
 * 将任务的 es（相对迭代起点的小时数）窗口映射到自然日历日，
 * 从而支撑“同一开发人单日总工时不得超过 8 小时”的过载校验。
 */

import { addWorkdays } from '../algorithm/workday.js'
import { SCHEDULE } from '../constants/enums.js'
import { assigneeLabel, type Task } from '../models/entities.js'

/** 单开发人单日负载 */
export interface DailyLoadEntry {
  /** 负责人 */
  assignee: string
  /** 日期（YYYY-MM-DD） */
  date: string
  /** 当日总工时 */
  hours: number
  /** 当日涉及的任务 ID */
  taskIds: string[]
}

/**
 * 计算每个开发人每天的负载。
 *
 * 约定：任务在 es 时刻开始，占用 estimatedHours 小时；工作日按“单日 8 小时”切分，
 * 跨天任务会自动分摊到多个自然日。
 */
export function computeDailyLoad(
  taskList: readonly Task[],
  iterationStartDate: string,
  workHoursPerDay: number = SCHEDULE.WORK_HOURS_PER_DAY,
): DailyLoadEntry[] {
  const buckets = new Map<string, DailyLoadEntry>()
  const perDay = workHoursPerDay > 0 ? workHoursPerDay : SCHEDULE.WORK_HOURS_PER_DAY

  const push = (assignee: string, date: string, hours: number, taskId: string): void => {
    const key = `${assignee}@${date}`
    const existing = buckets.get(key)
    if (existing) {
      existing.hours = Math.round((existing.hours + hours) * 100) / 100
      if (!existing.taskIds.includes(taskId)) existing.taskIds.push(taskId)
    } else {
      buckets.set(key, { assignee, date, hours: Math.round(hours * 100) / 100, taskIds: [taskId] })
    }
  }

  for (const task of taskList) {
    const assignee = assigneeLabel(task)
    let remaining = task.estimatedHours
    let cursorHour = Math.max(0, task.es)
    // 防御：工时非正时跳过
    if (!(remaining > 0)) continue
    while (remaining > 1e-9) {
      const dayIndex = Math.floor(cursorHour / perDay)
      const hourInDay = cursorHour % perDay
      const availInDay = perDay - hourInDay
      const consume = Math.min(remaining, availInDay)
      push(assignee, addWorkdays(iterationStartDate, dayIndex), consume, task.taskId)
      remaining -= consume
      cursorHour += consume
    }
  }

  return [...buckets.values()].sort((a, b) => (a.date === b.date ? a.assignee.localeCompare(b.assignee) : a.date.localeCompare(b.date)))
}

/**
 * 检出人员过载（单日工时超过阈值）。
 * @param loads {@link computeDailyLoad} 的结果
 * @param threshold 单日工时阈值，默认 8 小时
 */
export function detectOverloads(
  loads: readonly DailyLoadEntry[],
  threshold: number = SCHEDULE.WORK_HOURS_PER_DAY,
): DailyLoadEntry[] {
  return loads.filter((entry) => entry.hours > threshold + SCHEDULE.EPSILON)
}
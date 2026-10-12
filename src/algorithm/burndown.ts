/**
 * 燃尽图原始数据生成（§二.4 / §四.10）。
 *
 * 口径说明：按“理想燃尽线”生成 time / remainingHours 数据点，供前端绘制燃尽图。
 * 采用工作日推进（跳过周末），数据点数量 = ceil(总工期 / 单日工时) + 1。
 */

import { SCHEDULE } from '../constants/enums.js'
import type { BurndownItem, Task } from '../models/entities.js'
import { addWorkdays } from './workday.js'

export interface BurndownOptions {
  /** 任务集合 */
  taskList: readonly Task[]
  /** 迭代起始日期（ISO） */
  iterationStartDate: string
  /** 关键路径总工期（小时） */
  totalDurationHours: number
  /** 单日工时，默认 8 小时 */
  workHoursPerDay?: number
}

/** 计算任务集合的总预估工时 */
export function sumEstimatedHours(taskList: readonly Task[]): number {
  return taskList.reduce((acc, t) => acc + (Number.isFinite(t.estimatedHours) ? t.estimatedHours : 0), 0)
}

/**
 * 生成燃尽图原始数据。
 * remainingHours(d) = 总预估工时 × (1 - d / 总天数)，d 为已过工作日数。
 */
export function buildBurndown(options: BurndownOptions): BurndownItem[] {
  const { taskList, iterationStartDate, totalDurationHours } = options
  const workHoursPerDay = options.workHoursPerDay ?? SCHEDULE.WORK_HOURS_PER_DAY
  const totalEstimatedHours = sumEstimatedHours(taskList)

  if (totalEstimatedHours <= 0) {
    return [{ time: iterationStartDate, remainingHours: 0 }]
  }

  // 总天数：以总工期折算工作日；总工期为 0 时至少 1 天，避免除零
  const days = Math.max(1, Math.ceil(totalDurationHours / workHoursPerDay))
  const points: BurndownItem[] = []
  for (let d = 0; d <= days; d += 1) {
    const remaining = totalEstimatedHours * (1 - d / days)
    points.push({
      time: addWorkdays(iterationStartDate, d),
      remainingHours: Math.max(0, Math.round(remaining * 100) / 100),
    })
  }
  return points
}
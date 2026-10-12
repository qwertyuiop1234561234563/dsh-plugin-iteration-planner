/**
 * 工时评估修正（§3.3）。
 *
 * 规则：
 *  1. P0 优先级任务，工时上浮 10%；
 *  2. TEST、DEPLOY 类型任务，统一增加 20% 缓冲；
 *  3. DB_DESIGN 类型任务，统一增加 15% 缓冲；
 *  4. 叠加口径（已与需求方确认）：按 {@link BUFFER_APPLY_ORDER} 固定顺序相乘。
 *  例：P0 的 TEST 任务 → hours × 1.2 × 1.1。
 */

import { BUFFER_APPLY_ORDER, BUFFER_RULES, type BufferRule } from '../constants/enums.js'
import { cloneTask, type Task } from '../models/entities.js'

/** 单条任务的缓冲修正明细 */
export interface BufferAdjustment {
  taskId: string
  taskName: string
  before: number
  after: number
  /** 命中的规则 key 列表，便于审计追溯 */
  appliedRules: string[]
}

export interface BufferResult {
  /** 修正后的任务列表（新对象，不修改入参） */
  tasks: Task[]
  /** 明细，仅包含真正发生变化的任务 */
  adjustments: BufferAdjustment[]
}

/** 判断规则是否命中任务 */
function matchRule(rule: BufferRule, task: Task): boolean {
  if (rule.taskType && rule.taskType !== task.taskType) return false
  if (rule.priority && rule.priority !== task.priority) return false
  return Boolean(rule.taskType || rule.priority)
}

/**
 * 对任务列表执行缓冲修正。
 * @param taskList 原始任务（按优先级/类型已就绪）
 * @param rules 规则表，默认 {@link BUFFER_RULES}
 * @param order 叠加顺序，默认 {@link BUFFER_APPLY_ORDER}
 */
export function applyHourBuffers(
  taskList: readonly Task[],
  rules: readonly BufferRule[] = BUFFER_RULES,
  order: readonly ('type' | 'priority')[] = BUFFER_APPLY_ORDER,
): BufferResult {
  const adjustments: BufferAdjustment[] = []
  const now = Date.now()

  const tasks = taskList.map((task) => {
    const before = task.estimatedHours
    let factor = 1
    const appliedRules: string[] = []

    for (const phase of order) {
      for (const rule of rules) {
        const isTypeRule = Boolean(rule.taskType)
        const isPriorityRule = Boolean(rule.priority)
        if (phase === 'type' && !isTypeRule) continue
        if (phase === 'priority' && !isPriorityRule) continue
        if (!matchRule(rule, task)) continue
        factor *= 1 + rule.ratio
        appliedRules.push(rule.key)
      }
    }

    if (appliedRules.length === 0) return cloneTask(task)

    // 保留两位小数，避免浮点误差进入报表
    const after = Math.round(before * factor * 100) / 100
    adjustments.push({ taskId: task.taskId, taskName: task.taskName, before, after, appliedRules })
    return { ...cloneTask(task), estimatedHours: after, updateTime: now }
  })

  return { tasks, adjustments }
}
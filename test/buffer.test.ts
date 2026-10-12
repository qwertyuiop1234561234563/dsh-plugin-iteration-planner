/**
 * 工时评估修正单元测试（§3.3）。
 *
 * 规则与叠加口径：
 *  - P0 上浮 10%；TEST / DEPLOY 上浮 20%；DB_DESIGN 上浮 15%；
 *  - 多规则命中时按固定顺序（先类型、后优先级）相乘，例：P0 的 TEST = hours × 1.2 × 1.1。
 */

import { describe, expect, it } from 'vitest'
import { Priority, TaskType } from '../src/constants/enums.js'
import { applyHourBuffers } from '../src/service/buffer.js'
import { makeTask, makeTasks } from './fixtures.js'

describe('工时缓冲修正', () => {
  it('P0 优先级：上浮 10%', () => {
    const { tasks } = applyHourBuffers([makeTask({ taskId: 'A', estimatedHours: 10, priority: Priority.P0 })])
    expect(tasks[0].estimatedHours).toBe(11)
  })

  it('TEST 类型：上浮 20%', () => {
    const { tasks } = applyHourBuffers([makeTask({ taskId: 'A', estimatedHours: 10, taskType: TaskType.TEST })])
    expect(tasks[0].estimatedHours).toBe(12)
  })

  it('DEPLOY 类型：上浮 20%', () => {
    const { tasks } = applyHourBuffers([makeTask({ taskId: 'A', estimatedHours: 10, taskType: TaskType.DEPLOY })])
    expect(tasks[0].estimatedHours).toBe(12)
  })

  it('DB_DESIGN 类型：上浮 15%', () => {
    const { tasks } = applyHourBuffers([makeTask({ taskId: 'A', estimatedHours: 10, taskType: TaskType.DB_DESIGN })])
    expect(tasks[0].estimatedHours).toBe(11.5)
  })

  it('多规则命中：P0 的 TEST = 10 × 1.2 × 1.1 = 13.2', () => {
    const { tasks, adjustments } = applyHourBuffers([
      makeTask({ taskId: 'A', estimatedHours: 10, taskType: TaskType.TEST, priority: Priority.P0 }),
    ])
    expect(tasks[0].estimatedHours).toBe(13.2)
    // 命中的规则 key 便于审计追溯
    expect(adjustments[0].appliedRules).toEqual(['TYPE_TEST', 'PRIORITY_P0'])
  })

  it('多规则命中：P0 的 DB_DESIGN = 10 × 1.15 × 1.1 = 12.65', () => {
    const { tasks } = applyHourBuffers([
      makeTask({ taskId: 'A', estimatedHours: 10, taskType: TaskType.DB_DESIGN, priority: Priority.P0 }),
    ])
    expect(tasks[0].estimatedHours).toBe(12.65)
  })

  it('未命中任何规则的任务保持原值，且不产生修正明细', () => {
    const { tasks, adjustments } = applyHourBuffers([
      makeTask({ taskId: 'A', estimatedHours: 7, taskType: TaskType.BUSINESS_CODE, priority: Priority.P2 }),
    ])
    expect(tasks[0].estimatedHours).toBe(7)
    expect(adjustments).toHaveLength(0)
  })

  it('不修改入参对象（纯函数语义）', () => {
    const origin = makeTask({ taskId: 'A', estimatedHours: 10, priority: Priority.P0 })
    applyHourBuffers([origin])
    expect(origin.estimatedHours).toBe(10)
  })

  it('批量：仅发生变化的任务进入 adjustments', () => {
    const { adjustments } = applyHourBuffers(
      makeTasks([
        { taskId: 'A', estimatedHours: 10, priority: Priority.P0 },
        { taskId: 'B', estimatedHours: 5, priority: Priority.P3 },
      ]),
    )
    expect(adjustments.map((a) => a.taskId)).toEqual(['A'])
  })
})
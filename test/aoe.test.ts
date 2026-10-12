/**
 * AOE 关键路径单元测试（§三.2）与 §7.4 交付物要求。
 *
 * 覆盖场景：
 *  1. 正常场景：线性链 —— 全链均为关键任务；
 *  2. 正常场景：并行分支 —— 关键路径取最长分支，松弛分支 slack > 0；
 *  3. 孤立节点场景：多个无依赖任务，取最长者构成关键路径；
 *  4. 边界：空任务集、存在环（拓扑序不完整）。
 *
 * 口径（已与需求方确认）：
 *  - 项目总工期 totalDurationHours = max(所有任务 es + estimatedHours)，避免并行关键分支重复计工；
 *  - 汇点（无后置依赖）的 ls = 总工期 - 自身工时（否则汇点 slack 恒等于自身工时，必然漏判关键任务）。
 */

import { describe, expect, it } from 'vitest'
import { buildDag, kahnCheck } from '../src/algorithm/dag.js'
import { computeSchedule } from '../src/algorithm/aoe.js'
import { makeTasks } from './fixtures.js'

/** 便捷封装：构建图 → Kahn → AOE，贴近服务层真实调用顺序 */
function scheduleOf(seeds: Parameters<typeof makeTasks>[0]) {
  const tasks = makeTasks(seeds)
  const dag = kahnCheck(buildDag(tasks))
  return { tasks, dag, schedule: computeSchedule(tasks, dag.topoOrder) }
}

describe('AOE 关键路径（es / ls / slack / 总工期）', () => {
  it('空任务集：返回全零结果', () => {
    const schedule = computeSchedule([])
    expect(schedule.totalDurationHours).toBe(0)
    expect(schedule.criticalPath).toEqual([])
    expect(schedule.criticalTaskIds).toEqual([])
    expect(schedule.incomplete).toBe(false)
  })

  it('正常场景：线性链 A(2h)->B(3h)->C(4h) 全链关键，总工期 9h', () => {
    const { schedule } = scheduleOf([
      { taskId: 'A', estimatedHours: 2 },
      { taskId: 'B', estimatedHours: 3, predecessorTaskIds: ['A'] },
      { taskId: 'C', estimatedHours: 4, predecessorTaskIds: ['B'] },
    ])

    expect(schedule.esMap).toEqual({ A: 0, B: 2, C: 5 })
    expect(schedule.lsMap).toEqual({ A: 0, B: 2, C: 5 })
    expect(schedule.slackMap).toEqual({ A: 0, B: 0, C: 0 })
    expect(schedule.totalDurationHours).toBe(9)
    expect(schedule.criticalPath).toEqual(['A', 'B', 'C'])
    expect([...schedule.criticalTaskIds].sort()).toEqual(['A', 'B', 'C'])
  })

  it('正常场景：并行分支取最长分支为关键路径，另一分支 slack > 0', () => {
    // A(2) -> B(3) -> D(1)
    //      \-> C(5) -/
    const { schedule } = scheduleOf([
      { taskId: 'A', estimatedHours: 2 },
      { taskId: 'B', estimatedHours: 3, predecessorTaskIds: ['A'] },
      { taskId: 'C', estimatedHours: 5, predecessorTaskIds: ['A'] },
      { taskId: 'D', estimatedHours: 1, predecessorTaskIds: ['B', 'C'] },
    ])

    expect(schedule.esMap).toEqual({ A: 0, B: 2, C: 2, D: 7 })
    // 总工期 = max(es+工时) = D 的 8h（而非关键分支工时简单相加）
    expect(schedule.totalDurationHours).toBe(8)
    // B 不在关键路径上：ls_B = ls_D - 3 = 4，slack_B = 4 - 2 = 2
    expect(schedule.slackMap.B).toBe(2)
    expect(schedule.slackMap.A).toBe(0)
    expect(schedule.slackMap.C).toBe(0)
    expect(schedule.slackMap.D).toBe(0)
    expect(schedule.criticalPath).toEqual(['A', 'C', 'D'])
  })

  it('孤立节点场景：多个无依赖任务，关键路径取工时最长者', () => {
    const { schedule } = scheduleOf([
      { taskId: 'A', estimatedHours: 2 },
      { taskId: 'B', estimatedHours: 3 },
    ])

    // 总工期取最大值而非求和
    expect(schedule.totalDurationHours).toBe(3)
    // A 为汇点：ls_A = 3 - 2 = 1，slack_A = 1
    expect(schedule.slackMap.A).toBe(1)
    expect(schedule.slackMap.B).toBe(0)
    expect(schedule.criticalTaskIds).toEqual(['B'])
    expect(schedule.criticalPath).toEqual(['B'])
  })

  it('边界：存在环时 incomplete=true，不抛出（由调用方先行阻断）', () => {
    const tasks = makeTasks([
      { taskId: 'A', estimatedHours: 1, predecessorTaskIds: ['B'] },
      { taskId: 'B', estimatedHours: 1, predecessorTaskIds: ['A'] },
    ])
    const dag = kahnCheck(buildDag(tasks))
    expect(dag.hasCycle).toBe(true)
    const schedule = computeSchedule(tasks, dag.topoOrder)
    expect(schedule.incomplete).toBe(true)
  })
})
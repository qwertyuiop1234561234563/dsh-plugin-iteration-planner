/**
 * 风险检测单元测试（§四.8 硬编码规则，对应 §4.1~§4.4）。
 *
 * 通过真实的「Kahn + AOE + 日历负载」流水线驱动规则，而非手工构造中间结果，
 * 以验证规则在端到端数据下的判定行为。
 */

import { describe, expect, it } from 'vitest'
import { computeSchedule } from '../src/algorithm/aoe.js'
import { buildDag, kahnCheck } from '../src/algorithm/dag.js'
import { RiskLevel, TaskStatus } from '../src/constants/enums.js'
import { computeDailyLoad, detectOverloads } from '../src/service/calendar.js'
import { runHardcodedRiskRules } from '../src/service/risk.js'
import type { RiskItem } from '../src/models/entities.js'
import { makeTasks, type TaskSeed } from './fixtures.js'

/** 固定迭代起始日为周一，避免周末造成负载日期漂移 */
const START = '2026-10-12'

/** 跑完整硬编码风险规则链 */
function risksOf(seeds: readonly TaskSeed[]): RiskItem[] {
  const taskList = makeTasks(seeds)
  const graph = buildDag(taskList)
  const dag = kahnCheck(graph)
  const schedule = computeSchedule(taskList, dag.topoOrder)
  const loads = computeDailyLoad(taskList, START, 8)
  const overloads = detectOverloads(loads, 8)
  return runHardcodedRiskRules({ taskList, graph, dag, schedule, overloads })
}

describe('硬编码风险规则', () => {
  it('无风险场景：正常依赖链不产生任何风险', () => {
    const risks = risksOf([
      { taskId: 'A', estimatedHours: 2, assignee: 'dev-1' },
      { taskId: 'B', estimatedHours: 3, assignee: 'dev-2', predecessorTaskIds: ['A'] },
    ])
    expect(risks).toHaveLength(0)
  })

  it('规则 1：循环依赖 → HIGH，输出环内任务', () => {
    const risks = risksOf([
      { taskId: 'A', estimatedHours: 1, predecessorTaskIds: ['B'] },
      { taskId: 'B', estimatedHours: 1, predecessorTaskIds: ['A'] },
    ])
    const cycle = risks.find((r) => r.level === RiskLevel.HIGH)
    expect(cycle).toBeDefined()
    expect([...cycle!.taskIds].sort()).toEqual(['A', 'B'])
    expect(cycle!.suggest).toContain('依赖')
  })

  it('规则 2：人员过载（同一人同日 12h > 8h）→ MEDIUM，关联两个任务', () => {
    const risks = risksOf([
      { taskId: 'R', estimatedHours: 1, assignee: 'dev-2' },
      { taskId: 'A', estimatedHours: 6, assignee: 'dev-1', predecessorTaskIds: ['R'] },
      { taskId: 'B', estimatedHours: 6, assignee: 'dev-1', predecessorTaskIds: ['R'] },
    ])
    expect(risks).toHaveLength(1)
    const overload = risks[0]
    expect(overload.level).toBe(RiskLevel.MEDIUM)
    expect([...overload.taskIds].sort()).toEqual(['A', 'B'])
    expect(overload.description).toContain('dev-1')
  })

  it('规则 3：关键路径任务 DELAYED → HIGH', () => {
    const risks = risksOf([
      { taskId: 'A', estimatedHours: 2, assignee: 'dev-1' },
      { taskId: 'B', estimatedHours: 3, assignee: 'dev-2', predecessorTaskIds: ['A'], status: TaskStatus.DELAYED },
    ])
    expect(risks).toHaveLength(1)
    expect(risks[0].level).toBe(RiskLevel.HIGH)
    expect(risks[0].taskIds).toEqual(['B'])
  })

  it('规则 4：无前置无后置的孤立任务 → LOW', () => {
    const risks = risksOf([{ taskId: 'X', estimatedHours: 2, assignee: 'dev-1' }])
    expect(risks).toHaveLength(1)
    expect(risks[0].level).toBe(RiskLevel.LOW)
    expect(risks[0].taskIds).toEqual(['X'])
  })

  it('组合场景：过载 + 孤立可同时检出，等级互不覆盖', () => {
    const risks = risksOf([
      { taskId: 'A', estimatedHours: 6, assignee: 'dev-1' },
      { taskId: 'B', estimatedHours: 6, assignee: 'dev-1' },
    ])
    const levels = risks.map((r) => r.level).sort()
    expect(levels).toEqual([RiskLevel.LOW, RiskLevel.MEDIUM])
  })
})
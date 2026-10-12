/**
 * DAG 层单元测试（§三.1 Kahn 拓扑排序）与 §7.4 交付物要求。
 *
 * 覆盖场景：
 *  1. 正常 DAG（线性链 / 分叉汇聚）—— 拓扑顺序正确；
 *  2. 环场景 —— hasCycle=true 且输出环内节点；
 *  3. 孤立节点场景 —— 既不误判为环，又能被孤立任务规则检出；
 *  4. 悬空依赖 —— 引用不存在的 taskId 时不影响拓扑，且可被单独提示。
 */

import { describe, expect, it } from 'vitest'
import { buildDag, findDanglingPredecessors, findIsolatedTasks, kahnCheck } from '../src/algorithm/dag.js'
import { makeTasks } from './fixtures.js'

describe('Kahn 拓扑排序与环检测', () => {
  it('正常场景：线性链 A->B->C 输出完整拓扑序，无环', () => {
    const tasks = makeTasks([
      { taskId: 'A' },
      { taskId: 'B', predecessorTaskIds: ['A'] },
      { taskId: 'C', predecessorTaskIds: ['B'] },
    ])
    const graph = buildDag(tasks)
    // 入度：A=0, B=1, C=1；邻接：A->[B], B->[C]
    expect(graph.inDegree).toEqual({ A: 0, B: 1, C: 1 })
    expect(graph.adjList.A).toEqual(['B'])
    expect(graph.adjList.B).toEqual(['C'])

    const result = kahnCheck(graph)
    expect(result.hasCycle).toBe(false)
    expect(result.cycleNodes).toEqual([])
    expect(result.topoOrder).toEqual(['A', 'B', 'C'])
  })

  it('正常场景：分叉 + 汇聚的拓扑顺序满足“前置先于后置”', () => {
    const tasks = makeTasks([
      { taskId: 'root' },
      { taskId: 'api', predecessorTaskIds: ['root'] },
      { taskId: 'db', predecessorTaskIds: ['root'] },
      { taskId: 'deploy', predecessorTaskIds: ['api', 'db'] },
    ])
    const result = kahnCheck(buildDag(tasks))
    expect(result.hasCycle).toBe(false)
    expect(result.topoOrder).toHaveLength(4)

    const pos = new Map(result.topoOrder.map((id, i) => [id, i]))
    expect(pos.get('root')!).toBeLessThan(pos.get('api')!)
    expect(pos.get('root')!).toBeLessThan(pos.get('db')!)
    expect(pos.get('api')!).toBeLessThan(pos.get('deploy')!)
    expect(pos.get('db')!).toBeLessThan(pos.get('deploy')!)
  })

  it('环场景：A->B->C->A 判定存在环并输出全部环内节点', () => {
    const tasks = makeTasks([
      { taskId: 'A', predecessorTaskIds: ['C'] },
      { taskId: 'B', predecessorTaskIds: ['A'] },
      { taskId: 'C', predecessorTaskIds: ['B'] },
    ])
    const result = kahnCheck(buildDag(tasks))
    expect(result.hasCycle).toBe(true)
    expect(result.topoOrder).toHaveLength(0)
    expect([...result.cycleNodes].sort()).toEqual(['A', 'B', 'C'])
  })

  it('环场景：环 + 环外正常节点，仅环及其下游被标为 cycleNodes', () => {
    const tasks = makeTasks([
      { taskId: 'X', predecessorTaskIds: ['Y'] },
      { taskId: 'Y', predecessorTaskIds: ['X'] },
      { taskId: 'free' },
    ])
    const result = kahnCheck(buildDag(tasks))
    expect(result.hasCycle).toBe(true)
    expect([...result.cycleNodes].sort()).toEqual(['X', 'Y'])
    // free 入度为 0，先出队，因此出现在拓扑序中
    expect(result.topoOrder).toEqual(['free'])
  })

  it('孤立节点场景：无前置无后置不构成环，可被孤立任务规则检出', () => {
    const tasks = makeTasks([
      { taskId: 'A' },
      { taskId: 'B', predecessorTaskIds: ['A'] },
      { taskId: 'orphan' },
    ])
    const graph = buildDag(tasks)
    const result = kahnCheck(graph)

    expect(result.hasCycle).toBe(false)
    expect(result.topoOrder).toHaveLength(3)
    // 孤立任务仅 orphan（A 有后置 B）
    expect(findIsolatedTasks(tasks, graph)).toEqual(['orphan'])
  })

  it('孤立节点场景：链中节点均不被误判为孤立', () => {
    const tasks = makeTasks([
      { taskId: 'A' },
      { taskId: 'B', predecessorTaskIds: ['A'] },
    ])
    const graph = buildDag(tasks)
    expect(findIsolatedTasks(tasks, graph)).toEqual([])
  })

  it('悬空依赖：引用不存在的 taskId 时忽略入度，并可被单独提示', () => {
    const tasks = makeTasks([
      { taskId: 'A' },
      { taskId: 'B', predecessorTaskIds: ['A', 'ghost'] },
    ])
    const graph = buildDag(tasks)
    // ghost 不参与统计
    expect(graph.inDegree.B).toBe(1)
    expect(kahnCheck(graph).hasCycle).toBe(false)
    expect(findDanglingPredecessors(tasks)).toEqual([{ taskId: 'B', missing: ['ghost'] }])
  })
})
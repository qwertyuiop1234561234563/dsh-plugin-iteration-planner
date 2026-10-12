/**
 * 关键路径计算（AOE 网算法）—— §三.2。
 *
 * 强制要求：保持 AOE 正向/反向遍历语义，不得替换算法。
 *
 * 口径修正（已与需求方确认）：
 * - 项目总工期 totalDurationHours = max(所有任务 es + estimatedHours)，
 *   而非“关键路径上所有任务工时之和”（后者在多条并行关键分支时会重复计工）。
 * - criticalPath 通过“回溯最长路径”得到一条有序关键路径；
 *   同时输出 criticalTaskIds（所有 slack≈0 的任务集合）以备报表使用。
 */

import { SCHEDULE } from '../constants/enums.js'
import type { Task } from '../models/entities.js'

/** AOE 排期计算结果（§四.7 schedule_calc 的返回子集） */
export interface ScheduleResult {
  /** 有序关键路径（一条最长路径） */
  criticalPath: string[]
  /** 全部松弛时间为 0 的关键任务集合 */
  criticalTaskIds: string[]
  /** taskId -> 最早开始时间 es */
  esMap: Record<string, number>
  /** taskId -> 最晚开始时间 ls */
  lsMap: Record<string, number>
  /** taskId -> 松弛时间 slack */
  slackMap: Record<string, number>
  /** 关键路径总工期（小时） */
  totalDurationHours: number
  /** 拓扑序不完整（存在环）时为 true，调用方应先阻断 */
  incomplete: boolean
}

/**
 * 计算 es / ls / slack / 关键路径 / 总工期。
 *
 * @param taskList 任务集合
 * @param topoOrder dag_check 产出的拓扑顺序；缺失或长度不足时按任务自身顺序兜底
 */
export function computeSchedule(taskList: readonly Task[], topoOrder?: readonly string[]): ScheduleResult {
  if (taskList.length === 0) {
    return {
      criticalPath: [],
      criticalTaskIds: [],
      esMap: {},
      lsMap: {},
      slackMap: {},
      totalDurationHours: 0,
      incomplete: false,
    }
  }

  const byId = new Map(taskList.map((t) => [t.taskId, t]))
  const hours = (id: string): number => byId.get(id)?.estimatedHours ?? 0

  // 拓扑序兜底：dag_check 未提供有效序（含存在环时返回的残缺序）时，退化为任务列表顺序。
  // incomplete 用于提示调用方「拓扑序不完整 ⇒ 存在环」，必须依据传入的 topoOrder 判定，
  // 而不能依据兜底后的 order 长度（兜底后长度恒等于任务数）。
  const topoProvided = topoOrder !== undefined
  const order = topoProvided && topoOrder.length === taskList.length ? [...topoOrder] : taskList.map((t) => t.taskId)
  const incomplete = topoProvided && topoOrder.length !== taskList.length

  // 反向邻接：前置 -> 后置，用于反向遍历
  const successors: Record<string, string[]> = {}
  for (const task of taskList) successors[task.taskId] = []
  for (const task of taskList) {
    for (const predId of task.predecessorTaskIds) {
      if (successors[predId]) successors[predId].push(task.taskId)
    }
  }

  /* ---------------------- 1. 正向遍历：计算最早开始时间 es ---------------------- */
  const esMap: Record<string, number> = {}
  for (const id of order) esMap[id] = 0
  for (const id of order) {
    const task = byId.get(id)
    if (!task) continue
    let es = 0
    for (const predId of task.predecessorTaskIds) {
      if (esMap[predId] === undefined) continue // 悬空依赖：忽略
      es = Math.max(es, esMap[predId] + hours(predId))
    }
    esMap[id] = es
  }

  /* ------------------------- 2. 项目总工期 = max(es + 工时) ---------------------- */
  let totalDurationHours = 0
  for (const id of order) {
    totalDurationHours = Math.max(totalDurationHours, esMap[id] + hours(id))
  }

  /* ---------------------- 3. 反向遍历：计算最晚开始时间 ls ---------------------- */
  const lsMap: Record<string, number> = {}
  const reverseOrder = [...order].reverse()
  for (const id of reverseOrder) {
    const succ = successors[id] ?? []
    if (succ.length === 0) {
      // 无后置依赖（汇点）：ls = 项目总工期 - 自身工时。
      // 口径修正（已与需求方确认）：需求原文写「ls = 项目总工期」，但 AOE 中 ls 是「最晚开始时间」，
      // 汇点必须最晚在总工期时刻完成，故其最晚开始 = 总工期 - 自身工时；
      // 否则 slack = ls - es 恒等于自身工时，汇点永远无法被判为关键任务，关键路径必然缺尾。
      lsMap[id] = totalDurationHours - hours(id)
    } else {
      let ls = Number.POSITIVE_INFINITY
      for (const s of succ) {
        if (lsMap[s] === undefined) continue
        ls = Math.min(ls, lsMap[s] - hours(id))
      }
      lsMap[id] = Number.isFinite(ls) ? ls : totalDurationHours - hours(id)
    }
  }

  /* --------------------------- 4. 松弛时间 slack = ls - es ---------------------- */
  const slackMap: Record<string, number> = {}
  const criticalTaskIds: string[] = []
  for (const id of order) {
    const slack = lsMap[id] - esMap[id]
    slackMap[id] = slack
    if (Math.abs(slack) < SCHEDULE.EPSILON) criticalTaskIds.push(id)
  }

  /* ----------------------------- 5. 回溯最长路径 ------------------------------ */
  const criticalPath = reconstructLongestPath(order, byId, esMap, hours, totalDurationHours)

  return { criticalPath, criticalTaskIds, esMap, lsMap, slackMap, totalDurationHours, incomplete }
}

/**
 * 从“汇点”反向回溯出最长路径。
 * 选择规则（确定性）：在满足 es[pred]+hours[pred] == es[node] 的前置中，
 * 优先取工时更大者；再以优先级更高者优先；最后以 taskId 字典序兜底。
 */
function reconstructLongestPath(
  order: readonly string[],
  byId: Map<string, Task>,
  esMap: Record<string, number>,
  hours: (id: string) => number,
  totalDurationHours: number,
): string[] {
  // 找到达到总工期的终点（可能在多条关键分支上，取第一个即一条最长路径）
  const ends = order.filter((id) => Math.abs(esMap[id] + hours(id) - totalDurationHours) < SCHEDULE.EPSILON)
  if (ends.length === 0) return []

  const priorityRank = (id: string): number => {
    const p = byId.get(id)?.priority ?? 'P3'
    return ['P0', 'P1', 'P2', 'P3'].indexOf(p)
  }

  const path: string[] = []
  let cursor = ends[0]
  const guard = new Set<string>()
  while (cursor && !guard.has(cursor)) {
    guard.add(cursor)
    path.push(cursor)
    const task = byId.get(cursor)
    if (!task) break
    const candidates = task.predecessorTaskIds.filter(
      (predId) => esMap[predId] !== undefined && Math.abs(esMap[predId] + hours(predId) - esMap[cursor]) < SCHEDULE.EPSILON,
    )
    if (candidates.length === 0) break
    candidates.sort((a, b) => {
      const dh = hours(b) - hours(a)
      if (Math.abs(dh) > SCHEDULE.EPSILON) return dh
      const dp = priorityRank(a) - priorityRank(b)
      if (dp !== 0) return dp
      return a < b ? -1 : 1
    })
    cursor = candidates[0]
  }

  return path.reverse()
}
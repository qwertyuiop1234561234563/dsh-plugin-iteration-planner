/**
 * DAG 构建与环检测（Kahn 拓扑排序算法）—— §三.1。
 *
 * 强制要求：本文件实现必须保持 Kahn 算法语义，不得替换为 DFS 等其他算法。
 * 该层为纯函数，无 IO、无框架依赖，可被单元测试直接覆盖。
 */

import type { Task } from '../models/entities.js'

/** DAG 图结构：邻接表 + 入度表（§四.2 dag_build 的返回） */
export interface DagGraph {
  /** key=taskId，value=其后置任务 ID 列表 */
  adjList: Record<string, string[]>
  /** key=taskId，value=其入度 */
  inDegree: Record<string, number>
}

/** Kahn 校验结果（§四.3 dag_check 的返回） */
export interface DagCheckResult {
  /** 是否存在环 */
  hasCycle: boolean
  /** 环内任务列表（存在环时非空） */
  cycleNodes: string[]
  /** 拓扑排序后的任务顺序（无环时为全量顺序） */
  topoOrder: string[]
}

/**
 * 构建 DAG 图结构（§三.1 步骤 1）。
 *
 * 遍历全部任务构建邻接表 adjList，同时统计每个节点的入度 inDegree。
 * 注意：只统计“指向已知任务”的依赖；指向不存在任务的悬空引用会被忽略，
 * 由 {@link findDanglingPredecessors} 单独检出并交由风险层提示。
 */
export function buildDag(taskList: readonly Task[]): DagGraph {
  const adjList: Record<string, string[]> = {}
  const inDegree: Record<string, number> = {}
  const known = new Set(taskList.map((t) => t.taskId))

  // 初始化所有节点，保证孤立节点也出现在图中
  for (const task of taskList) {
    adjList[task.taskId] = []
    inDegree[task.taskId] = 0
  }

  // 建立有向边：前置任务 -> 当前任务
  for (const task of taskList) {
    for (const predId of task.predecessorTaskIds) {
      if (!known.has(predId)) continue // 悬空依赖，忽略入度统计
      adjList[predId].push(task.taskId)
      inDegree[task.taskId] += 1
    }
  }

  return { adjList, inDegree }
}

/**
 * Kahn 拓扑排序 + 环检测（§三.1 步骤 2~5）。
 *
 * 1. 将所有入度为 0 的节点入队；
 * 2. 循环出队，将其后置节点入度减 1，减为 0 则入队；
 * 3. 若最终排序数量 < 总任务数，则存在环，输出入度未归零的环内节点。
 */
export function kahnCheck(graph: DagGraph): DagCheckResult {
  const { adjList } = graph
  // 复制入度，避免修改调用方数据
  const inDegree: Record<string, number> = { ...graph.inDegree }
  const allNodes = Object.keys(adjList)

  // 初始化队列：所有入度为 0 的节点
  const queue: string[] = []
  for (const node of allNodes) {
    if (inDegree[node] === 0) queue.push(node)
  }

  const topoOrder: string[] = []
  // 使用索引游标模拟出队，避免 O(n^2) 的数组 shift
  for (let head = 0; head < queue.length; head += 1) {
    const current = queue[head]
    topoOrder.push(current)
    for (const next of adjList[current] ?? []) {
      inDegree[next] -= 1
      if (inDegree[next] === 0) {
        queue.push(next)
      }
    }
  }

  const hasCycle = topoOrder.length < allNodes.length
  // 环内节点：拓扑排序结束后入度仍 > 0 的节点（包含环及其下游）
  const cycleNodes = hasCycle ? allNodes.filter((node) => inDegree[node] > 0) : []

  return { hasCycle, cycleNodes, topoOrder }
}

/**
 * 检出悬空前置依赖（引用了不存在的 taskId）。
 * 该信息用于风险提示，不影响 Kahn 拓扑本身。
 */
export function findDanglingPredecessors(taskList: readonly Task[]): Array<{ taskId: string; missing: string[] }> {
  const known = new Set(taskList.map((t) => t.taskId))
  const result: Array<{ taskId: string; missing: string[] }> = []
  for (const task of taskList) {
    const missing = task.predecessorTaskIds.filter((id) => !known.has(id))
    if (missing.length > 0) {
      result.push({ taskId: task.taskId, missing })
    }
  }
  return result
}

/**
 * 判定孤立任务（§4.4）：既无前置依赖，也无任何后置依赖。
 * 需要图结构参与，故与环检测放在同一算法文件。
 */
export function findIsolatedTasks(taskList: readonly Task[], graph: DagGraph): string[] {
  // 先收集「拥有至少一个后置任务」的节点
  const hasSuccessor = new Set<string>()
  for (const [node, successors] of Object.entries(graph.adjList)) {
    if (successors.length > 0) hasSuccessor.add(node)
  }
  return taskList
    .filter((t) => t.predecessorTaskIds.length === 0 && !hasSuccessor.has(t.taskId))
    .map((t) => t.taskId)
}
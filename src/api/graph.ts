/**
 * DAG 视图数据装配：把计划快照转换为前端画布（AntV X6）可直接渲染的节点/边结构。
 *
 * 图计算完全复用算法层的 Kahn 拓扑实现（§三.1），此处仅做数据整形，不含任何算法逻辑。
 */

import { buildDag, findDanglingPredecessors, findIsolatedTasks, kahnCheck } from '../algorithm/index.js'
import type { ProjectPlan, Task } from '../models/entities.js'

/** 画布节点 */
export interface GraphNode {
  id: string
  label: string
  taskType: Task['taskType']
  priority: Task['priority']
  status: Task['status']
  assignee?: string
  estimatedHours: number
  es: number
  ls: number
  slack: number
  /** 是否关键路径任务（slack=0） */
  critical: boolean
  /** 是否孤立任务（无前置且无后置） */
  isolated: boolean
  /** 是否处于循环依赖中 */
  inCycle: boolean
}

/** 画布边（前置 → 后置） */
export interface GraphEdge {
  source: string
  target: string
}

/** DAG 视图载荷 */
export interface GraphPayload {
  nodes: GraphNode[]
  edges: GraphEdge[]
  hasCycle: boolean
  cycleNodes: string[]
  topoOrder: string[]
  isolatedTasks: string[]
  dangling: Array<{ taskId: string; missing: string[] }>
}

/** 由计划构造 DAG 视图载荷 */
export function buildGraphPayload(plan: ProjectPlan): GraphPayload {
  const taskList = plan.taskList
  const graph = buildDag(taskList)
  const dag = kahnCheck(graph)
  const isolated = new Set(findIsolatedTasks(taskList, graph))
  const cycle = new Set(dag.cycleNodes)
  const critical = new Set(plan.criticalPath)

  const edges: GraphEdge[] = []
  for (const [source, targets] of Object.entries(graph.adjList)) {
    for (const target of targets) edges.push({ source, target })
  }

  const nodes: GraphNode[] = taskList.map((task) => ({
    id: task.taskId,
    label: task.taskName,
    taskType: task.taskType,
    priority: task.priority,
    status: task.status,
    assignee: task.assignee,
    estimatedHours: task.estimatedHours,
    es: task.es,
    ls: task.ls,
    slack: task.slack,
    // 关键路径以算法层返回的 criticalPath 为准，避免浮点误差导致误判
    critical: critical.has(task.taskId),
    isolated: isolated.has(task.taskId),
    inCycle: cycle.has(task.taskId),
  }))

  return {
    nodes,
    edges,
    hasCycle: dag.hasCycle,
    cycleNodes: dag.cycleNodes,
    topoOrder: dag.topoOrder,
    isolatedTasks: [...isolated],
    dangling: findDanglingPredecessors(taskList),
  }
}
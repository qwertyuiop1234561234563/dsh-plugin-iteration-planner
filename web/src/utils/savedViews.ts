/**
 * 任务高级筛选器与保存视图。
 *
 * 定位：**纯展示层筛选**，条件全部基于引擎返回的任务字段（状态 / 类型 / 优先级 /
 * 工时 / 责任人 / 松弛时间等），并允许叠加「关键路径 / 风险命中 / 里程碑 / 孤立节点」
 * 这些由引擎或只读推断给出的标记，不参与任何业务判定与写操作。
 *
 * 保存视图持久化在 localStorage：视图只保存**筛选条件**，不保存任务集合，
 * 因此计划发生重规划后视图依然有效（条件重算而非快照）。
 */

import type { Priority, ProjectPlan, Task, TaskType, TaskStatus } from '@/api/types'

/** 任务筛选条件 */
export interface TaskFilter {
  /** 关键词（匹配任务名 / 描述 / 责任人 / 任务 ID） */
  keyword: string
  taskType: TaskType | ''
  status: TaskStatus | ''
  priority: Priority | ''
  assignee: string
  minHours: number | null
  maxHours: number | null
  /** 仅看关键路径任务 */
  onlyCritical: boolean
  /** 仅看命中风险的任务 */
  onlyRisky: boolean
  /** 仅看里程碑任务 */
  onlyMilestone: boolean
  /** 仅看无前置且无后继的孤立任务 */
  onlyIsolated: boolean
}

/** 默认（空）筛选条件 */
export function emptyFilter(): TaskFilter {
  return {
    keyword: '',
    taskType: '',
    status: '',
    priority: '',
    assignee: '',
    minHours: null,
    maxHours: null,
    onlyCritical: false,
    onlyRisky: false,
    onlyMilestone: false,
    onlyIsolated: false,
  }
}

/** 已保存视图 */
export interface SavedView {
  id: string
  name: string
  filter: TaskFilter
}

const STORAGE_KEY = 'iteration-planner:task-views'

/** 筛选上下文：由调用方从计划派生（关键路径集合 / 风险任务集合 / 里程碑集合） */
export interface FilterContext {
  criticalIds: Set<string>
  riskyIds: Set<string>
  milestoneIds: Set<string>
  isolatedIds: Set<string>
}

/** 判断筛选条件是否为空（用于界面上「已启用 N 项筛选」提示） */
export function activeFilterCount(filter: TaskFilter): number {
  let count = 0
  if (filter.keyword.trim()) count += 1
  if (filter.taskType) count += 1
  if (filter.status) count += 1
  if (filter.priority) count += 1
  if (filter.assignee) count += 1
  if (filter.minHours !== null || filter.maxHours !== null) count += 1
  if (filter.onlyCritical) count += 1
  if (filter.onlyRisky) count += 1
  if (filter.onlyMilestone) count += 1
  if (filter.onlyIsolated) count += 1
  return count
}

/** 按条件筛选任务（所有条件为 AND 关系） */
export function filterTasks(tasks: Task[], filter: TaskFilter, context: FilterContext): Task[] {
  const keyword = filter.keyword.trim().toLowerCase()
  return tasks.filter((task) => {
    if (keyword) {
      const haystack = `${task.taskName} ${task.taskDesc} ${task.assignee ?? ''} ${task.taskId}`.toLowerCase()
      if (!haystack.includes(keyword)) return false
    }
    if (filter.taskType && task.taskType !== filter.taskType) return false
    if (filter.status && task.status !== filter.status) return false
    if (filter.priority && task.priority !== filter.priority) return false
    if (filter.assignee && (task.assignee ?? '待分配') !== filter.assignee) return false
    if (filter.minHours !== null && task.estimatedHours < filter.minHours) return false
    if (filter.maxHours !== null && task.estimatedHours > filter.maxHours) return false
    if (filter.onlyCritical && !context.criticalIds.has(task.taskId)) return false
    if (filter.onlyRisky && !context.riskyIds.has(task.taskId)) return false
    if (filter.onlyMilestone && !context.milestoneIds.has(task.taskId)) return false
    if (filter.onlyIsolated && !context.isolatedIds.has(task.taskId)) return false
    return true
  })
}

/** 从计划与依赖关系派生筛选上下文 */
export function buildFilterContext(
  plan: ProjectPlan | null,
  milestoneIds: Set<string>,
  graphIsolated: string[] | undefined,
): FilterContext {
  const criticalIds = new Set(plan?.criticalPath ?? [])
  const riskyIds = new Set<string>()
  for (const risk of plan?.riskList ?? []) {
    for (const taskId of risk.taskIds) riskyIds.add(taskId)
  }
  let isolatedIds: Set<string>
  if (graphIsolated && graphIsolated.length > 0) {
    isolatedIds = new Set(graphIsolated)
  } else {
    // 退化口径：既无前置也无后继的任务（与引擎 isolated 判定一致）
    const referenced = new Set<string>()
    const hasPredecessor = new Set<string>()
    for (const task of plan?.taskList ?? []) {
      if (task.predecessorTaskIds.length > 0) hasPredecessor.add(task.taskId)
      for (const predecessor of task.predecessorTaskIds) referenced.add(predecessor)
    }
    isolatedIds = new Set(
      (plan?.taskList ?? [])
        .filter((task) => !hasPredecessor.has(task.taskId) && !referenced.has(task.taskId))
        .map((task) => task.taskId),
    )
  }
  return { criticalIds, riskyIds, milestoneIds, isolatedIds }
}

/** 读取已保存视图 */
export function readSavedViews(): SavedView[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw) as unknown
    if (!Array.isArray(parsed)) return []
    return parsed
      .filter((item): item is SavedView => {
        const candidate = item as Partial<SavedView>
        return typeof candidate?.id === 'string' && typeof candidate?.name === 'string' && !!candidate?.filter
      })
      .map((item) => ({ ...item, filter: { ...emptyFilter(), ...item.filter } }))
  } catch {
    return []
  }
}

/** 写入已保存视图 */
export function writeSavedViews(views: SavedView[]): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(views))
  } catch {
    // 忽略存储异常
  }
}

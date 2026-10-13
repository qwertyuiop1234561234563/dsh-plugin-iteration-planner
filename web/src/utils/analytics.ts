/**
 * 度量分析（展示层聚合）：累积流图 CFD、速度图 Velocity、里程碑识别。
 *
 * ⚠️ 定位说明：
 *  - 本模块**不产生任何业务口径**，只把引擎既有的返回（计划快照 / 燃尽数据）与
 *    审计日志（`/api/audit` 的 `task_update` 记录）整理成图表可用的形状；
 *  - 关键路径、工期、风险、排期等一律以引擎 `src/**` 的计算结果为准；
 *  - 里程碑是**展示性推断**（发布任务 / 末端交付任务），不参与排期与风险判定。
 */

import type { AuditRecord, BurndownItem, ProjectPlan, Task, TaskStatus } from '@/api/types'
import { STATUS_FLOW_ORDER } from '@/utils/format'
import { WORK_HOURS_PER_DAY, addWorkdays } from '@/utils/workday'

/* --------------------------------- 日期换算 -------------------------------- */

/** 任务在日历上的起止日期（按 es 与工时的**工作日**推进；仅用于日历 / 甘特展示） */
export function taskDateRange(plan: ProjectPlan, task: Task): { start: string; end: string } {
  const offsetDays = Math.floor(Math.max(0, task.es) / WORK_HOURS_PER_DAY)
  const spanDays = Math.max(1, Math.ceil(task.estimatedHours / WORK_HOURS_PER_DAY))
  const start = addWorkdays(plan.iterationStartDate, offsetDays)
  const end = addWorkdays(start, spanDays - 1)
  return { start, end }
}

/* --------------------------------- 累积流图 -------------------------------- */

/** 累积流图数据点 */
export interface CfdPoint {
  /** 时间戳（毫秒） */
  time: number
  /** 展示用时间标签 */
  label: string
  /** 各状态的任务数 */
  counts: Record<TaskStatus, number>
}

/** 空的状态计数桶 */
function emptyCounts(): Record<TaskStatus, number> {
  return { TODO: 0, IN_PROGRESS: 0, BLOCKED: 0, TESTING: 0, DONE: 0, DELAYED: 0 }
}

/** 按状态计数并写入 bucket */
function countInto(bucket: Record<TaskStatus, number>, status: TaskStatus): void {
  bucket[status] = (bucket[status] ?? 0) + 1
}

/**
 * 从审计记录中提取任务快照。
 *
 * 审计输出可能是对象（短计划）或**字符串**（超过长度上限被截断）。截断串里
 * `{"task":{...}}` 位于最前端且结构完整，因此用花括号配平精确截取 task 对象后再解析，
 * 避免把整个被截断的 JSON 直接 parse（必然失败）。
 */
function extractTaskSnapshot(output: unknown): { taskId: string; status: TaskStatus } | null {
  const readTask = (value: unknown): { taskId: string; status: TaskStatus } | null => {
    if (!value || typeof value !== 'object') return null
    const task = (value as { task?: unknown }).task
    if (!task || typeof task !== 'object') return null
    const candidate = task as { taskId?: unknown; status?: unknown }
    if (typeof candidate.taskId !== 'string' || typeof candidate.status !== 'string') return null
    return { taskId: candidate.taskId, status: candidate.status as TaskStatus }
  }

  if (typeof output === 'object') return readTask(output)
  if (typeof output !== 'string') return null

  const keyIndex = output.indexOf('"task"')
  const braceStart = keyIndex >= 0 ? output.indexOf('{', keyIndex) : -1
  if (braceStart < 0) return null

  let depth = 0
  let inString = false
  let escaped = false
  for (let index = braceStart; index < output.length; index += 1) {
    const char = output[index]
    if (inString) {
      if (escaped) escaped = false
      else if (char === '\\') escaped = true
      else if (char === '"') inString = false
      continue
    }
    if (char === '"') inString = true
    else if (char === '{') depth += 1
    else if (char === '}') {
      depth -= 1
      if (depth === 0) {
        try {
          return readTask(JSON.parse(output.slice(braceStart, index + 1)))
        } catch {
          return null
        }
      }
    }
  }
  return null
}

/** 审计记录中的一次状态变迁 */
interface StatusTransition {
  time: number
  taskId: string
  status: TaskStatus
}

/** 从审计记录中提取全部「状态」变迁（按时间升序） */
export function extractStatusTransitions(records: AuditRecord[]): StatusTransition[] {
  const result: StatusTransition[] = []
  for (const record of records) {
    if (!record.ok || record.action !== 'task_update') continue
    const input = record.input as { updateField?: unknown } | undefined
    if (input?.updateField !== 'status') continue
    const time = Date.parse(record.ts)
    if (Number.isNaN(time)) continue
    const snapshot = extractTaskSnapshot(record.output)
    if (!snapshot) continue
    result.push({ time, taskId: snapshot.taskId, status: snapshot.status })
  }
  return result.sort((a, b) => a.time - b.time)
}

/**
 * 派生累积流图：以「审计记录的状态变迁」重建各时点的任务状态分布。
 *
 * 基线口径：
 *  - 在有过变迁记录的任务，其基线状态视为 `TODO`（它们确实从待办开始流动）；
 *  - 从未有过变迁记录的任务，其基线状态取其当前状态（它们自始至终未变）；
 *  - 末端追加「当前时点」，用计划的真实状态收口，保证图形终点与现状一致。
 */
export function deriveCfd(plan: ProjectPlan | null, records: AuditRecord[]): CfdPoint[] {
  if (!plan || plan.taskList.length === 0) return []
  const transitions = extractStatusTransitions(records)
  const now = Date.now()

  if (transitions.length === 0) {
    const counts = emptyCounts()
    for (const task of plan.taskList) countInto(counts, task.status)
    return [{ time: now, label: formatPointLabel(now), counts }]
  }

  const changedIds = new Set(transitions.map((item) => item.taskId))
  const statusById = new Map<string, TaskStatus>()
  const counts = emptyCounts()
  for (const task of plan.taskList) {
    const baseline: TaskStatus = changedIds.has(task.taskId) ? 'TODO' : task.status
    statusById.set(task.taskId, baseline)
    countInto(counts, baseline)
  }

  const points: CfdPoint[] = [{ time: transitions[0].time - 1, label: formatPointLabel(transitions[0].time - 1), counts: { ...counts } }]

  for (const transition of transitions) {
    const previous = statusById.get(transition.taskId)
    if (!previous) continue
    if (previous === transition.status) continue
    counts[previous] -= 1
    counts[transition.status] += 1
    statusById.set(transition.taskId, transition.status)
    const last = points[points.length - 1]
    if (last.time === transition.time) {
      last.counts = { ...counts }
      continue
    }
    points.push({ time: transition.time, label: formatPointLabel(transition.time), counts: { ...counts } })
  }

  // 用当前真实状态收口（补齐「审计未覆盖但已变更」的任务）
  const finalCounts = emptyCounts()
  for (const task of plan.taskList) countInto(finalCounts, task.status)
  const tail = points[points.length - 1]
  if (tail.time !== now) points.push({ time: now, label: formatPointLabel(now), counts: finalCounts })
  else tail.counts = finalCounts

  return points
}

/** 时间戳 → `MM-DD HH:mm` */
function formatPointLabel(time: number): string {
  const date = new Date(time)
  const pad = (n: number): string => String(n).padStart(2, '0')
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/** 累积流图可用的状态序列（仅保留在数据中出现过的状态，避免空系列） */
export function cfdStatuses(points: CfdPoint[]): TaskStatus[] {
  const present = new Set<TaskStatus>()
  for (const point of points) {
    for (const status of STATUS_FLOW_ORDER) {
      if ((point.counts[status] ?? 0) > 0) present.add(status)
    }
  }
  return STATUS_FLOW_ORDER.filter((status) => present.has(status))
}

/* --------------------------------- 速度图 --------------------------------- */

/** 速度数据点 */
export interface VelocityPoint {
  day: string
  /** 当日完成工时（燃尽差值，来自引擎燃尽数据） */
  completed: number
  /** 累计完成工时 */
  cumulative: number
  /** 当日剩余工时 */
  remaining: number
}

/**
 * 派生速度图：直接取引擎燃尽数据逐日差分。
 * 燃尽数据的口径（工作日推进、缓冲修正后工时）完全由引擎决定，前端仅做差分展示。
 */
export function deriveVelocity(burndown: BurndownItem[]): VelocityPoint[] {
  if (burndown.length === 0) return []
  const points: VelocityPoint[] = []
  let cumulative = 0
  for (let index = 0; index < burndown.length; index += 1) {
    const item = burndown[index]
    const completed = index === 0 ? 0 : Math.max(0, burndown[index - 1].remainingHours - item.remainingHours)
    cumulative += completed
    points.push({ day: item.time, completed: Number(completed.toFixed(2)), cumulative: Number(cumulative.toFixed(2)), remaining: item.remainingHours })
  }
  return points
}

/** 速度摘要（用于 KPI 卡） */
export function velocitySummary(points: VelocityPoint[]): { totalCompleted: number; avgPerDay: number; remaining: number; activeDays: number } {
  if (points.length === 0) return { totalCompleted: 0, avgPerDay: 0, remaining: 0, activeDays: 0 }
  const activeDays = points.filter((point) => point.completed > 0).length
  const totalCompleted = points[points.length - 1].cumulative
  return {
    totalCompleted,
    avgPerDay: activeDays > 0 ? Number((totalCompleted / activeDays).toFixed(2)) : 0,
    remaining: points[points.length - 1].remaining,
    activeDays,
  }
}

/* --------------------------------- 里程碑 --------------------------------- */

/** 里程碑类型（展示性推断，不参与排期判定） */
export type MilestoneKind = 'RELEASE' | 'DELIVERY'

/** 里程碑 */
export interface Milestone {
  taskId: string
  taskName: string
  kind: MilestoneKind
  /** 里程碑所在日期（按工作日推进推算，仅用于展示） */
  date: string
  es: number
}

/**
 * 自动标记里程碑（**只读推断**）：
 *  - `RELEASE`：任务类型为「部署」的任务，对上线发布节点；
 *  - `DELIVERY`：没有任何后继任务的末端节点，对可交付节点。
 * 同一任务同时满足时优先记为发布里程碑。
 */
export function detectMilestones(plan: ProjectPlan | null): Milestone[] {
  if (!plan) return []
  const referenced = new Set<string>()
  for (const task of plan.taskList) {
    for (const predecessor of task.predecessorTaskIds) referenced.add(predecessor)
  }
  const milestones: Milestone[] = []
  for (const task of plan.taskList) {
    const isRelease = task.taskType === 'DEPLOY'
    const isDelivery = !referenced.has(task.taskId)
    if (!isRelease && !isDelivery) continue
    milestones.push({
      taskId: task.taskId,
      taskName: task.taskName,
      kind: isRelease ? 'RELEASE' : 'DELIVERY',
      date: taskDateRange(plan, task).end,
      es: task.es,
    })
  }
  return milestones.sort((a, b) => a.es - b.es || a.taskName.localeCompare(b.taskName))
}

/* --------------------------------- 任务健康度 ------------------------------- */

/** 任务健康度（表格行高亮 / 看板卡提示用，纯展示） */
export type TaskHealth = 'critical-overdue' | 'critical' | 'at-risk' | 'normal'

/** 判定任务健康度：延期 / 关键路径 / 松弛紧张 / 正常 */
export function taskHealth(task: Task, isCritical: boolean, deadline: string, plan: ProjectPlan | null): TaskHealth {
  if (task.status === 'DELAYED') return 'critical-overdue'
  if (plan) {
    const range = taskDateRange(plan, task)
    if (task.status !== 'DONE' && range.end > deadline) return 'critical-overdue'
  }
  if (isCritical) return 'critical'
  if (task.slack <= WORK_HOURS_PER_DAY) return 'at-risk'
  return 'normal'
}

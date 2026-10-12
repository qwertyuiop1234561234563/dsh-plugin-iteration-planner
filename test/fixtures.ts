/**
 * 测试夹具：构造稳定的 Task 实体，避免各测试文件重复样板代码。
 *
 * 说明：算法层为纯函数，测试直接以 Task 数组为输入，不涉及 Harness / Cordis 运行时。
 */

import { Priority, TaskStatus, TaskType } from '../src/constants/enums.js'
import { toTask, type Task } from '../src/models/entities.js'

/** 任务种子：仅需声明与断言相关的字段，其余取默认值 */
export interface TaskSeed {
  taskId: string
  taskName?: string
  taskType?: TaskType
  estimatedHours?: number
  predecessorTaskIds?: string[]
  status?: TaskStatus
  priority?: Priority
  assignee?: string
}

/** 由种子构造完整 Task（es/ls/slack 初始为 0，等待算法层计算） */
export function makeTask(seed: TaskSeed): Task {
  return toTask({
    taskId: seed.taskId,
    taskName: seed.taskName ?? seed.taskId,
    taskDesc: `${seed.taskId} 描述`,
    taskType: seed.taskType ?? TaskType.BUSINESS_CODE,
    estimatedHours: seed.estimatedHours ?? 1,
    predecessorTaskIds: seed.predecessorTaskIds ?? [],
    status: seed.status ?? TaskStatus.TODO,
    priority: seed.priority ?? Priority.P2,
    assignee: seed.assignee,
  })
}

/** 批量构造 */
export function makeTasks(seeds: readonly TaskSeed[]): Task[] {
  return seeds.map(makeTask)
}
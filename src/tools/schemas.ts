/**
 * 工具层共享 schema 与渲染工具。
 *
 * `defineTool` 要求 parameters 使用「类型化 schema DSL」（非裸 JSON Schema），
 * 且 output 必须返回 canonical JSON 值 + render 回调。
 * 本文件集中定义 Task 对象 / 数组参数片段，避免各工具重复。
 */

import type { ContentBlock } from '@deepseek-ai/dsh-llm'
import type { JsonValue } from '@deepseek-ai/dsh-util-values'
import { PRIORITY_VALUES, TASK_STATUS_VALUES, TASK_TYPE_VALUES } from '../constants/enums.js'
import type { TaskBase } from '../models/entities.js'

/**
 * Task 对象的参数 schema（作为数组 items 复用）。
 * 采用 `additionalProperties: true` 提升对模型输出的容忍度，
 * 真正的字段校验/规范化在服务层完成（§7.8 异常处理）。
 */
export const TASK_OBJECT_SPEC = {
  type: 'object',
  additionalProperties: true,
  properties: {
    taskId: { type: 'string', description: '任务唯一 ID，缺省时由系统生成' },
    taskName: { type: 'string', required: true, description: '任务名称' },
    taskDesc: { type: 'string', description: '任务详细描述' },
    taskType: { type: 'string', enum: TASK_TYPE_VALUES, description: '任务类型' },
    estimatedHours: { type: 'number', description: '预估工时（小时）' },
    predecessorTaskIds: { type: 'array', items: { type: 'string' }, description: '前置依赖 taskId 数组' },
    status: { type: 'string', enum: TASK_STATUS_VALUES, description: '任务状态' },
    priority: { type: 'string', enum: PRIORITY_VALUES, description: '优先级，P0 最高' },
    assignee: { type: 'string', description: '负责人（可选）' },
  },
} as const

/** 工具入参中的任务草稿结构（宽松，服务层负责规范化） */
export interface TaskDraftInput {
  taskId?: string
  taskName: string
  taskDesc?: string
  taskType?: string
  estimatedHours?: number
  predecessorTaskIds?: string[]
  status?: string
  priority?: string
  assignee?: string
}

/** 把模型传入的 JSON 片段安全转为任务草稿数组 */
export function toTaskDrafts(value: unknown): TaskDraftInput[] {
  if (!Array.isArray(value)) return []
  return value.filter((v): v is TaskDraftInput => !!v && typeof v === 'object' && typeof (v as TaskDraftInput).taskName === 'string')
}

/**
 * 任务草稿 → `toTask` 入参的类型边界。
 *
 * 模型传入的 `taskType`/`status`/`priority` 是宽松字符串（schema 用 enum 约束但运行时仍是 string），
 * 而 `TaskBase` 使用枚举类型。此处做一次显式的类型收窄：
 * 枚举合法性由 `toTask` 的缺省回退与 `task_update` 的服务层校验共同保证，
 * 避免把 `unknown` 校验散落到每个工具里（§7.8）。
 */
export function asTaskInput(draft: TaskDraftInput): Partial<TaskBase> & Pick<TaskBase, 'taskName'> {
  return draft as unknown as Partial<TaskBase> & Pick<TaskBase, 'taskName'>
}

/** 统一的文本渲染：把 canonical JSON 值序列化为模型可读文本 */
export function textBlocks(value: unknown): ContentBlock[] {
  const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2)
  return [{ type: 'text', text }]
}

/** 统一的 output 声明（canonical 值为任意 JSON） */
export const JSON_OUTPUT = {
  schema: { type: 'json' },
} as const

/**
 * 把领域对象安全转为 canonical JSON 值。
 * defineTool 要求 execute 返回 lossless JSON；此处通过 JSON 序列化边界消除
 * 领域接口（无索引签名）与 JsonValue 之间的结构性类型差异。
 */
export function toJson(value: unknown): JsonValue {
  return JSON.parse(JSON.stringify(value)) as JsonValue
}
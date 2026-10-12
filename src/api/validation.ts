/**
 * 请求参数校验（zod）。
 *
 * 所有入参在进入业务层之前完成结构校验，校验失败返回 400 并附带字段级明细，
 * 避免非法数据流入算法层（§7.8 明确错误提示）。
 * 枚举取值统一引用 `src/constants/enums.ts`，不硬编码字符串。
 */

import { z } from 'zod'
import { PRIORITY_VALUES, TASK_STATUS_VALUES, TASK_TYPE_VALUES } from '../constants/enums.js'
import { apiErrors } from './errors.js'
import type { UpdatableField } from '../service/planner-service.js'

/** ISO 日期（YYYY-MM-DD） */
export const isoDateSchema = z
  .string()
  .regex(/^\d{4}-\d{2}-\d{2}$/, '日期格式必须为 YYYY-MM-DD')

/** 由常量数组生成枚举校验（保证与 enums.ts 单一事实来源一致） */
function enumOf<T extends string>(values: readonly T[], label: string) {
  return z.custom<T>((value) => typeof value === 'string' && (values as readonly string[]).includes(value), {
    message: `${label} 取值必须是：${values.join(' / ')}`,
  })
}

/** 任务可更新字段白名单（与 §四.5 对齐） */
export const UPDATABLE_FIELDS: readonly UpdatableField[] = [
  'estimatedHours',
  'predecessorTaskIds',
  'status',
  'priority',
  'assignee',
]

/** 新建计划入参（§四.1 task_decompose + 全局约束） */
export const createPlanSchema = z.object({
  requirement: z.string().min(1, '需求内容不能为空'),
  iterationDeadline: isoDateSchema,
  iterationStartDate: isoDateSchema.optional(),
  devMemberCount: z.coerce.number().int('开发人数必须为整数').positive('开发人数必须大于 0'),
})

/** 新增任务入参（§四.4） */
export const taskDraftSchema = z.object({
  taskId: z.string().min(1).optional(),
  taskName: z.string().min(1, '任务名称不能为空'),
  taskDesc: z.string().optional(),
  taskType: enumOf(TASK_TYPE_VALUES, 'taskType').optional(),
  estimatedHours: z.coerce.number().positive('预估工时必须大于 0').optional(),
  predecessorTaskIds: z.array(z.string()).optional(),
  status: enumOf(TASK_STATUS_VALUES, 'status').optional(),
  priority: enumOf(PRIORITY_VALUES, 'priority').optional(),
  assignee: z.string().optional(),
})

/** 修改任务入参（§四.5） */
export const taskUpdateSchema = z.object({
  updateField: z.enum(['estimatedHours', 'predecessorTaskIds', 'status', 'priority', 'assignee']),
  newValue: z.unknown(),
})

/** 重规划入参（§四.9 / §3.5） */
export const replanSchema = z.object({
  iterationDeadline: isoDateSchema.optional(),
  iterationStartDate: isoDateSchema.optional(),
  devMemberCount: z.coerce.number().int().positive().optional(),
})

/** 审计日志查询入参 */
export const auditQuerySchema = z.object({
  planId: z.string().min(1).optional(),
  limit: z.coerce.number().int().positive().max(2000).optional(),
})

/** 校验结果类型 */
export type CreatePlanBody = z.infer<typeof createPlanSchema>
export type TaskDraftBody = z.infer<typeof taskDraftSchema>
export type TaskUpdateBody = z.infer<typeof taskUpdateSchema>
export type ReplanBody = z.infer<typeof replanSchema>
export type AuditQuery = z.infer<typeof auditQuerySchema>

/**
 * 执行校验；失败时抛出携带字段明细的 400 异常。
 * 该函数让路由层保持扁平，不必重复 try/catch。
 */
export function parseOrThrow<T>(schema: z.ZodType<T>, value: unknown, label: string): T {
  const result = schema.safeParse(value)
  if (result.success) return result.data
  const issues = result.error.issues.map((issue) => ({
    field: issue.path.join('.') || '(root)',
    message: issue.message,
  }))
  throw apiErrors.badRequest(`${label}校验失败`, issues)
}
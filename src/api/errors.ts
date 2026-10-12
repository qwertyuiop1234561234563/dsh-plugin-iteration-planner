/**
 * API 异常模型与统一错误映射（§7.8 明确错误提示，不静默崩溃）。
 *
 * 领域异常 → HTTP 语义的映射规则：
 *  - 循环依赖            → 409 Conflict（业务冲突，用户需修正依赖）
 *  - 计划状态非法        → 400 Bad Request
 *  - LLM 输出格式错误    → 422 Unprocessable Entity
 *  - 仓储 / 数据库异常   → 503 Service Unavailable
 *  - 入参校验失败        → 400
 *  - 资源不存在          → 404
 *  - 未授权              → 401
 *  - 其它未知异常        → 500（不吞异常，明确提示）
 */

import { LlmFormatError } from '../llm/types.js'
import { RepositoryError } from '../repository/postgres.js'
import { PlanCycleError, PlanStateError } from '../service/planner-service.js'

/** 统一错误响应体 */
export interface ApiErrorBody {
  /** 机器可读错误码 */
  code: string
  /** 人类可读错误信息 */
  message: string
  /** 附加细节（例如环内任务列表、字段校验明细） */
  details?: unknown
}

/** 业务可预期的 API 异常 */
export class ApiError extends Error {
  constructor(
    readonly statusCode: number,
    readonly code: string,
    message: string,
    readonly details?: unknown,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

/** 常用错误构造快捷方法 */
export const apiErrors = {
  badRequest: (message: string, details?: unknown) => new ApiError(400, 'VALIDATION_ERROR', message, details),
  unauthorized: (message = '缺少或无效的访问令牌') => new ApiError(401, 'UNAUTHORIZED', message),
  notFound: (message: string) => new ApiError(404, 'NOT_FOUND', message),
  conflict: (message: string, details?: unknown) => new ApiError(409, 'CONFLICT', message, details),
  internal: (message = '服务内部错误') => new ApiError(500, 'INTERNAL_ERROR', message),
} as const

/** 领域异常 → {@link ApiError} */
export function toApiError(error: unknown): ApiError {
  if (error instanceof ApiError) return error

  // 循环依赖：明确的业务性失败，返回环内任务供前端高亮
  if (error instanceof PlanCycleError) {
    return new ApiError(409, 'CYCLE_DEPENDENCY', error.message, { cycleNodes: error.cycleNodes })
  }
  // 计划状态非法（任务不存在、字段非法、ID 冲突等）
  if (error instanceof PlanStateError) {
    return new ApiError(400, 'PLAN_STATE_ERROR', error.message)
  }
  // LLM 输出格式错误：可恢复，提示用户重试或调整需求描述
  if (error instanceof LlmFormatError) {
    return new ApiError(422, 'LLM_FORMAT_ERROR', error.message)
  }
  // 仓储 / 数据库异常：依赖不可用
  if (error instanceof RepositoryError) {
    return new ApiError(503, 'REPOSITORY_ERROR', error.message)
  }

  const message = error instanceof Error ? error.message : String(error)
  return new ApiError(500, 'INTERNAL_ERROR', `服务内部错误：${message}`)
}

/** 将异常转换为标准响应体 */
export function toErrorBody(error: ApiError): ApiErrorBody {
  return { code: error.code, message: error.message, details: error.details }
}
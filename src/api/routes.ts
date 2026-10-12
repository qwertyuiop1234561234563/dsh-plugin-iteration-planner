/**
 * REST 路由注册（§四 工具能力 → HTTP 语义化）。
 *
 * 约定：
 *  - 所有入参经 {@link parseOrThrow} 校验，失败返回 400 + 字段明细；
 *  - 领域异常由全局错误处理器统一映射（见 `server.ts` 的 `setErrorHandler`）；
 *  - 写操作（新建 / 增删改任务 / 重规划）成功后由会话层落库并通过 WebSocket 广播。
 */

import type { FastifyInstance, FastifyReply, FastifyRequest } from 'fastify'
import { BUFFER_RULES, PRIORITY_VALUES, RISK_RULE, TASK_STATUS_VALUES, TASK_TYPE_VALUES } from '../constants/enums.js'
import { PlanCycleError } from '../service/planner-service.js'
import { buildReport } from '../service/report.js'
import type { ApiConfig } from './config.js'
import type { PlanEventBus } from './events.js'
import { buildGraphPayload } from './graph.js'
import type { PlanSessionManager } from './plan-session.js'
import type { AuditRepository } from '../repository/types.js'
import { UPDATABLE_FIELDS, auditQuerySchema, createPlanSchema, parseOrThrow, replanSchema, taskDraftSchema, taskUpdateSchema } from './validation.js'

/** 路由依赖 */
export interface ApiRouteDeps {
  config: ApiConfig
  manager: PlanSessionManager
  auditRepository: AuditRepository
  events: PlanEventBus
  /** 存储类型标识 */
  storageKind: 'memory' | 'postgres'
  /** 服务启动时间戳（健康检查计算 uptime） */
  startedAt: number
  /** 应用版本号 */
  version: string
}

/** 统一成功响应包装 */
function ok<T>(data: T): { data: T } {
  return { data }
}

/** 读取路径参数 */
function planIdOf(request: FastifyRequest): string {
  return (request.params as { planId: string }).planId
}

/** 从请求中提取任务 ID */
function taskIdOf(request: FastifyRequest): string {
  return (request.params as { taskId: string }).taskId
}

/**
 * 注册全部 API 路由。
 * 路由前缀取自 `config.basePath`（默认 /api），便于与前端静态资源共域部署。
 */
export function registerApiRoutes(app: FastifyInstance, deps: ApiRouteDeps): void {
  const { config, manager, auditRepository, events } = deps
  const base = config.basePath

  /* ------------------------------- 系统级 ------------------------------- */

  app.get(
    `${base}/health`,
    { schema: { tags: ['system'], summary: '健康检查' } },
    async () => ({
      status: 'ok',
      version: deps.version,
      storage: deps.storageKind,
      uptimeMs: Date.now() - deps.startedAt,
      wsSubscribers: events.size,
    }),
  )

  app.get(
    `${base}/meta/enums`,
    { schema: { tags: ['system'], summary: '枚举元数据（供前端下拉选项）' } },
    async () =>
      ok({
        taskType: TASK_TYPE_VALUES,
        taskStatus: TASK_STATUS_VALUES,
        priority: PRIORITY_VALUES,
        bufferRules: BUFFER_RULES,
        riskRules: RISK_RULE,
        updatableFields: UPDATABLE_FIELDS,
      }),
  )

  /* ------------------------------- 计划级 ------------------------------- */

  app.post(
    `${base}/plans`,
    {
      schema: {
        tags: ['plan'],
        summary: '新建迭代计划（拆解→缓冲→DAG→AOE→风险→燃尽）',
        description: '对应工具 plan_iteration：完整跑通规划流水线并持久化。',
      },
    },
    async (request, reply) => {
      const body = parseOrThrow(createPlanSchema, request.body, '新建计划入参')
      const plan = await manager.create({
        requirement: body.requirement,
        constraints: {
          iterationDeadline: body.iterationDeadline,
          iterationStartDate: body.iterationStartDate,
          devMemberCount: body.devMemberCount,
        },
      })
      reply.code(201)
      return ok(plan)
    },
  )

  app.get(
    `${base}/plans`,
    { schema: { tags: ['plan'], summary: '计划列表（摘要）' } },
    async () => ok(await manager.list()),
  )

  app.get(
    `${base}/plans/:planId`,
    { schema: { tags: ['plan'], summary: '计划详情（完整快照）' } },
    async (request) => ok(await manager.getPlan(planIdOf(request))),
  )

  app.delete(
    `${base}/plans/:planId`,
    { schema: { tags: ['plan'], summary: '删除计划' } },
    async (request, reply) => {
      await manager.remove(planIdOf(request))
      reply.code(204)
      return null
    },
  )

  app.post(
    `${base}/plans/:planId/replan`,
    {
      schema: {
        tags: ['plan'],
        summary: '全量重规划（可更新迭代截止日期 / 起始日期 / 开发人数）',
        description: '对应工具 replan_all：按 §3.5 固定顺序重跑校验 → 排期 → 风险 → 燃尽。',
      },
    },
    async (request) => {
      const body = parseOrThrow(replanSchema, request.body ?? {}, '重规划入参')
      const session = await manager.get(planIdOf(request))
      return ok(await session.replan(body))
    },
  )

  /* ------------------------------- 任务级 ------------------------------- */

  app.get(
    `${base}/plans/:planId/tasks`,
    { schema: { tags: ['task'], summary: '任务清单' } },
    async (request) => ok((await manager.getPlan(planIdOf(request))).taskList),
  )

  app.post(
    `${base}/plans/:planId/tasks`,
    { schema: { tags: ['task'], summary: '新增任务（自动全量重规划）' } },
    async (request, reply) => {
      const body = parseOrThrow(taskDraftSchema, request.body, '新增任务入参')
      const session = await manager.get(planIdOf(request))
      const result = await session.addTask(body)
      reply.code(201)
      return ok(result)
    },
  )

  app.patch(
    `${base}/plans/:planId/tasks/:taskId`,
    {
      schema: {
        tags: ['task'],
        summary: '修改任务字段（自动全量重规划）',
        description: 'updateField 支持：estimatedHours / predecessorTaskIds / status / priority / assignee。',
      },
    },
    async (request) => {
      const body = parseOrThrow(taskUpdateSchema, request.body, '修改任务入参')
      const session = await manager.get(planIdOf(request))
      return ok(await session.updateTask(taskIdOf(request), body.updateField, body.newValue))
    },
  )

  app.delete(
    `${base}/plans/:planId/tasks/:taskId`,
    { schema: { tags: ['task'], summary: '删除任务（同时清理引用并重规划）' } },
    async (request) => {
      const session = await manager.get(planIdOf(request))
      return ok(await session.deleteTask(taskIdOf(request)))
    },
  )

  /* --------------------------- 视图 / 报表 / 审计 --------------------------- */

  app.get(
    `${base}/plans/:planId/graph`,
    {
      schema: {
        tags: ['view'],
        summary: 'DAG 视图数据（节点 / 边 / 关键路径 / 环 / 孤立任务）',
      },
    },
    async (request) => ok(buildGraphPayload(await manager.getPlan(planIdOf(request)))),
  )

  app.get(
    `${base}/plans/:planId/critical-path`,
    { schema: { tags: ['view'], summary: '关键路径（taskId 有序数组）' } },
    async (request) => {
      const plan = await manager.getPlan(planIdOf(request))
      return ok({ criticalPath: plan.criticalPath, totalDurationHours: plan.totalDurationHours })
    },
  )

  app.get(
    `${base}/plans/:planId/risks`,
    { schema: { tags: ['view'], summary: '风险清单' } },
    async (request) => ok((await manager.getPlan(planIdOf(request))).riskList),
  )

  app.get(
    `${base}/plans/:planId/burndown`,
    { schema: { tags: ['view'], summary: '燃尽图原始数据' } },
    async (request) => ok((await manager.getPlan(planIdOf(request))).burndownRawData),
  )

  app.get(
    `${base}/plans/:planId/report`,
    {
      schema: {
        tags: ['view'],
        summary: '迭代报表（Markdown 文本 + 燃尽数据）',
        description: '对应工具 report_generate。',
      },
    },
    async (request) => {
      const plan = await manager.getPlan(planIdOf(request))
      const report = buildReport(plan)
      return ok({ format: 'markdown', content: report.text, burndownRawData: report.burndownRawData })
    },
  )

  app.get(
    `${base}/plans/:planId/export`,
    { schema: { tags: ['view'], summary: '导出计划（format=json|md）' } },
    async (request: FastifyRequest, reply: FastifyReply) => {
      const planId = planIdOf(request)
      const format = ((request.query as { format?: string }).format ?? 'json').toLowerCase()
      const plan = await manager.getPlan(planId)
      if (format === 'md' || format === 'markdown') {
        reply
          .header('content-type', 'text/markdown; charset=utf-8')
          .header('content-disposition', `attachment; filename="iteration-plan-${planId}.md"`)
        return buildReport(plan).text
      }
      reply
        .header('content-type', 'application/json; charset=utf-8')
        .header('content-disposition', `attachment; filename="iteration-plan-${planId}.json"`)
      return JSON.stringify(plan, null, 2)
    },
  )

  app.get(
    `${base}/plans/:planId/audit`,
    { schema: { tags: ['audit'], summary: '该计划的全链路审计日志' } },
    async (request) => {
      const { limit } = parseOrThrow(auditQuerySchema, request.query, '审计查询入参')
      return ok(await auditRepository.list({ planId: planIdOf(request), limit }))
    },
  )

  app.get(
    `${base}/audit`,
    { schema: { tags: ['audit'], summary: '全局审计日志（可按计划过滤）' } },
    async (request) => {
      const query = parseOrThrow(auditQuerySchema, request.query, '审计查询入参')
      return ok(await auditRepository.list({ planId: query.planId, limit: query.limit }))
    },
  )

  /* -------------------------- 循环依赖校验（显式） -------------------------- */

  app.post(
    `${base}/plans/:planId/dag-check`,
    { schema: { tags: ['view'], summary: '显式执行 Kahn 拓扑校验' } },
    async (request) => {
      const plan = await manager.getPlan(planIdOf(request))
      const payload = buildGraphPayload(plan)
      if (payload.hasCycle) {
        throw new PlanCycleError(payload.cycleNodes)
      }
      return ok({ hasCycle: false, topoOrder: payload.topoOrder })
    },
  )
}
/**
 * API 集成测试（不监听端口，使用 `fastify.inject` + 内存仓储）。
 *
 * 覆盖：
 *  1. 健康检查与元数据接口；
 *  2. 计划创建 → 查询 → 列表（完整规划流水线）；
 *  3. 任务新增 / 修改（触发全量重规划与关键路径延期风险）；
 *  4. 循环依赖阻断（409 CYCLE_DEPENDENCY + 环内任务）；
 *  5. 入参校验失败（400 + 字段明细）与资源不存在（404）；
 *  6. Bearer Token 鉴权（401）；
 *  7. DAG 视图 / 燃尽 / 报表导出。
 *
 * 说明：这些用例只依赖新增的 API 层与仓储层，不修改任何算法层 / 服务层实现，
 * 与既有的 33 项算法与服务层单测相互独立、可并行执行。
 */

import type { FastifyInstance } from 'fastify'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import type { RuntimeLogger } from '../src/api/audit-bridge.js'
import type { ApiConfig } from '../src/api/config.js'
import { createApiRuntime, type ApiRuntime } from '../src/api/runtime.js'
import { buildServer } from '../src/api/server.js'
import { TaskStatus } from '../src/constants/enums.js'
import type { ProjectPlan } from '../src/models/entities.js'

/** 静默运行日志：测试期间不污染输出（审计仍会写入内存仓储，可被断言） */
const silentLogger: RuntimeLogger = { info: () => {}, error: () => {} }

/** 测试用配置（内存仓储、关闭 Swagger、静默日志） */
function baseConfig(overrides: Partial<ApiConfig> = {}): ApiConfig {
  return {
    host: '127.0.0.1',
    port: 0,
    basePath: '/api',
    corsOrigin: '*',
    storageDriver: 'memory',
    postgres: {},
    wsPath: '/ws',
    logLevel: 'silent',
    enableSwagger: false,
    fallbackToRule: true,
    workHoursPerDay: 8,
    overloadThreshold: 8,
    ...overrides,
  }
}

/** 测试夹具：运行时 + Fastify 实例 */
interface Fixture {
  runtime: ApiRuntime
  app: FastifyInstance
}

async function createFixture(overrides: Partial<ApiConfig> = {}): Promise<Fixture> {
  const config = baseConfig(overrides)
  const runtime = await createApiRuntime({ config, version: 'test', logger: silentLogger })
  const app = await buildServer({
    config: runtime.config,
    bundle: runtime.bundle,
    manager: runtime.manager,
    events: runtime.events,
    version: runtime.version,
  })
  await app.ready()
  return { runtime, app }
}

const REQUIREMENT = '为电商中台开发订单中心迭代：完成订单创建、支付回调与库存扣减，对接第三方支付网关，并产出接口文档。'

let fx: Fixture
let plan: ProjectPlan

beforeAll(async () => {
  fx = await createFixture()
  const response = await fx.app.inject({
    method: 'POST',
    url: '/api/plans',
    payload: {
      requirement: REQUIREMENT,
      iterationDeadline: '2026-10-30',
      iterationStartDate: '2026-10-12',
      devMemberCount: 3,
    },
  })
  expect(response.statusCode).toBe(201)
  plan = response.json<{ data: ProjectPlan }>().data
})

afterAll(async () => {
  await fx.app.close()
  await fx.runtime.close()
})

describe('系统接口', () => {
  it('健康检查返回存储类型与版本', async () => {
    const response = await fx.app.inject({ method: 'GET', url: '/api/health' })
    expect(response.statusCode).toBe(200)
    const body = response.json<{ status: string; storage: string; version: string }>()
    expect(body.status).toBe('ok')
    expect(body.storage).toBe('memory')
    expect(body.version).toBe('test')
  })

  it('枚举元数据与常量文件一致', async () => {
    const response = await fx.app.inject({ method: 'GET', url: '/api/meta/enums' })
    expect(response.statusCode).toBe(200)
    const body = response.json<{ data: { taskType: string[]; updatableFields: string[] } }>().data
    expect(body.taskType).toContain('DB_DESIGN')
    expect(body.updatableFields).toContain('estimatedHours')
  })

  it('未知路由返回 404 NOT_FOUND', async () => {
    const response = await fx.app.inject({ method: 'GET', url: '/api/not-exist' })
    expect(response.statusCode).toBe(404)
    expect(response.json<{ code: string }>().code).toBe('NOT_FOUND')
  })
})

describe('计划生命周期', () => {
  it('新建计划产出完整规划结果', () => {
    expect(plan.planId).toBeTruthy()
    expect(plan.taskList.length).toBeGreaterThan(0)
    expect(plan.criticalPath.length).toBeGreaterThan(0)
    expect(plan.totalDurationHours).toBeGreaterThan(0)
    // es/ls/slack 由 AOE 算法层填充
    expect(plan.taskList.every((t) => typeof t.slack === 'number')).toBe(true)
    // 关键路径任务 slack 必须为 0
    const critical = plan.taskList.filter((t) => plan.criticalPath.includes(t.taskId))
    expect(critical.every((t) => Math.abs(t.slack) < 1e-6)).toBe(true)
  })

  it('计划详情与列表可查询', async () => {
    const detail = await fx.app.inject({ method: 'GET', url: `/api/plans/${plan.planId}` })
    expect(detail.statusCode).toBe(200)
    expect(detail.json<{ data: ProjectPlan }>().data.planId).toBe(plan.planId)

    const list = await fx.app.inject({ method: 'GET', url: '/api/plans' })
    const summaries = list.json<{ data: Array<{ planId: string; taskCount: number }> }>().data
    expect(summaries.some((s) => s.planId === plan.planId)).toBe(true)
  })

  it('DAG 视图返回节点、边与拓扑信息且无环', async () => {
    const response = await fx.app.inject({ method: 'GET', url: `/api/plans/${plan.planId}/graph` })
    expect(response.statusCode).toBe(200)
    const graph = response.json<{
      data: { nodes: unknown[]; edges: unknown[]; hasCycle: boolean; topoOrder: string[]; isolatedTasks: string[] }
    }>().data
    expect(graph.nodes).toHaveLength(plan.taskList.length)
    expect(graph.hasCycle).toBe(false)
    expect(graph.topoOrder).toHaveLength(plan.taskList.length)
    expect(graph.edges.length).toBeGreaterThan(0)
    expect(Array.isArray(graph.isolatedTasks)).toBe(true)
  })

  it('燃尽数据与报表导出可用', async () => {
    const burndown = await fx.app.inject({ method: 'GET', url: `/api/plans/${plan.planId}/burndown` })
    expect(burndown.statusCode).toBe(200)
    expect(burndown.json<{ data: unknown[] }>().data.length).toBeGreaterThan(0)

    const report = await fx.app.inject({ method: 'GET', url: `/api/plans/${plan.planId}/report` })
    expect(report.statusCode).toBe(200)
    expect(report.json<{ data: { content: string } }>().data.content).toContain('迭代规划报告')

    const exported = await fx.app.inject({ method: 'GET', url: `/api/plans/${plan.planId}/export?format=md` })
    expect(exported.statusCode).toBe(200)
    expect(exported.headers['content-type']).toContain('text/markdown')
  })
})

describe('任务变更与重规划', () => {
  it('新增任务后任务数与审计日志同步增长', async () => {
    const before = plan.taskList.length
    const response = await fx.app.inject({
      method: 'POST',
      url: `/api/plans/${plan.planId}/tasks`,
      payload: { taskName: '补充：对账定时任务', estimatedHours: 4, priority: 'P1' },
    })
    expect(response.statusCode).toBe(201)
    const result = response.json<{ data: { task: { taskId: string }; plan: ProjectPlan } }>().data
    expect(result.plan.taskList).toHaveLength(before + 1)
    expect(result.task.taskId).toBeTruthy()

    const audit = await fx.app.inject({ method: 'GET', url: `/api/plans/${plan.planId}/audit` })
    expect(audit.statusCode).toBe(200)
    const records = audit.json<{ data: Array<{ action: string }> }>().data
    expect(records.map((r) => r.action)).toContain('task_add')

    // 清理，避免影响后续用例
    await fx.app.inject({ method: 'DELETE', url: `/api/plans/${plan.planId}/tasks/${result.task.taskId}` })
  })

  it('把关键路径任务置为 DELAYED 会触发 HIGH 级延期风险', async () => {
    const criticalId = plan.criticalPath[0]
    const response = await fx.app.inject({
      method: 'PATCH',
      url: `/api/plans/${plan.planId}/tasks/${criticalId}`,
      payload: { updateField: 'status', newValue: TaskStatus.DELAYED },
    })
    expect(response.statusCode).toBe(200)
    const updated = response.json<{ data: { task: { status: string }; plan: ProjectPlan } }>().data
    expect(updated.task.status).toBe(TaskStatus.DELAYED)
    const delayRisk = updated.plan.riskList.find((r) => r.level === 'HIGH' && r.taskIds.includes(criticalId))
    expect(delayRisk).toBeTruthy()
    expect(delayRisk?.suggest).toBeTruthy()

    // 还原状态，避免影响其它用例
    await fx.app.inject({
      method: 'PATCH',
      url: `/api/plans/${plan.planId}/tasks/${criticalId}`,
      payload: { updateField: 'status', newValue: TaskStatus.TODO },
    })
  })

  it('修改前置依赖构成环时返回 409 且给出环内任务', async () => {
    const first = plan.taskList[0].taskId
    const last = plan.taskList[plan.taskList.length - 1].taskId
    const response = await fx.app.inject({
      method: 'PATCH',
      url: `/api/plans/${plan.planId}/tasks/${first}`,
      payload: { updateField: 'predecessorTaskIds', newValue: [last] },
    })
    expect(response.statusCode).toBe(409)
    const body = response.json<{ code: string; details: { cycleNodes: string[] } }>()
    expect(body.code).toBe('CYCLE_DEPENDENCY')
    expect(body.details.cycleNodes.length).toBeGreaterThan(0)

    // 恢复无依赖，确保后续用例可继续
    await fx.app.inject({
      method: 'PATCH',
      url: `/api/plans/${plan.planId}/tasks/${first}`,
      payload: { updateField: 'predecessorTaskIds', newValue: [] },
    })
  })

  it('重规划可更新全局约束', async () => {
    const response = await fx.app.inject({
      method: 'POST',
      url: `/api/plans/${plan.planId}/replan`,
      payload: { devMemberCount: 5, iterationDeadline: '2026-11-15' },
    })
    expect(response.statusCode).toBe(200)
    const replanned = response.json<{ data: ProjectPlan }>().data
    expect(replanned.devMemberCount).toBe(5)
    expect(replanned.iterationDeadline).toBe('2026-11-15')
    expect(replanned.planId).toBe(plan.planId)
  })
})

describe('入参与鉴权', () => {
  it('缺少必填字段返回 400 与字段明细', async () => {
    const response = await fx.app.inject({ method: 'POST', url: '/api/plans', payload: { requirement: '' } })
    expect(response.statusCode).toBe(400)
    const body = response.json<{ code: string; details: Array<{ field: string }> }>()
    expect(body.code).toBe('VALIDATION_ERROR')
    expect(body.details.length).toBeGreaterThan(0)
  })

  it('非法 updateField 被拒绝', async () => {
    const response = await fx.app.inject({
      method: 'PATCH',
      url: `/api/plans/${plan.planId}/tasks/${plan.taskList[0].taskId}`,
      payload: { updateField: 'taskName', newValue: 'x' },
    })
    expect(response.statusCode).toBe(400)
    expect(response.json<{ code: string }>().code).toBe('VALIDATION_ERROR')
  })

  it('不存在的计划返回 404', async () => {
    const response = await fx.app.inject({ method: 'GET', url: '/api/plans/plan-not-exist' })
    expect(response.statusCode).toBe(404)
  })

  it('配置 API_TOKEN 后未携带令牌返回 401', async () => {
    const guarded = await createFixture({ apiToken: 'secret-token' })
    try {
      const denied = await guarded.app.inject({ method: 'GET', url: '/api/plans' })
      expect(denied.statusCode).toBe(401)
      expect(denied.json<{ code: string }>().code).toBe('UNAUTHORIZED')

      const allowed = await guarded.app.inject({
        method: 'GET',
        url: '/api/plans',
        headers: { authorization: 'Bearer secret-token' },
      })
      expect(allowed.statusCode).toBe(200)

      // 健康检查不受鉴权影响（供探针使用）
      const health = await guarded.app.inject({ method: 'GET', url: '/api/health' })
      expect(health.statusCode).toBe(200)
    } finally {
      await guarded.app.close()
      await guarded.runtime.close()
    }
  })
})
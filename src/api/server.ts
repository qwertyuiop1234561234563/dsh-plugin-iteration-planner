/**
 * API 服务装配（Fastify 实例构建）。
 *
 * 同一套装配逻辑同时服务两种运行形态：
 *  - 独立进程：`src/api/standalone.ts` 直接调用 {@link buildServer}；
 *  - Cordis 插件：`src/api/plugin.ts` 在 Harness 内调用 {@link buildServer} 并托管生命周期。
 *
 * 中间件清单：CORS、Swagger(OpenAPI + UI)、静态资源托管（前端 SPA）、WebSocket 事件推送、
 * Bearer Token 鉴权（可选）、统一错误处理与 SPA 兜底路由。
 */

import { existsSync } from 'node:fs'
import cors from '@fastify/cors'
import fastifyStatic from '@fastify/static'
import swagger from '@fastify/swagger'
import swaggerUi from '@fastify/swagger-ui'
import websocket from '@fastify/websocket'
import Fastify, { type FastifyInstance } from 'fastify'
import type { RepositoryBundle } from '../repository/index.js'
import type { ApiConfig } from './config.js'
import { apiErrors, toApiError, toErrorBody } from './errors.js'
import type { PlanEventBus } from './events.js'
import type { PlanSessionManager } from './plan-session.js'
import { registerApiRoutes } from './routes.js'

/** 构建参数 */
export interface BuildServerDeps {
  config: ApiConfig
  /** 仓储聚合（用于审计查询与存储类型标识） */
  bundle: RepositoryBundle
  manager: PlanSessionManager
  events: PlanEventBus
  version: string
}

/** 启动结果：持有 Fastify 实例，供调用方关闭 */
export interface ServerHandle {
  app: FastifyInstance
  /** 实际监听地址（端口为 0 时用于回读系统分配端口） */
  address: string
  close(): Promise<void>
}

/** 构造 Fastify 实例（不监听端口，便于测试使用 `app.inject`） */
export async function buildServer(deps: BuildServerDeps): Promise<FastifyInstance> {
  const { config } = deps
  const app = Fastify({
    logger: { level: config.logLevel },
    // 请求体上限 2MB：需求文本 + 任务数组足够，避免大包打满内存
    bodyLimit: 2 * 1024 * 1024,
  })

  /* ------------------------------- CORS ------------------------------- */
  await app.register(cors, {
    origin: config.corsOrigin === '*' ? true : config.corsOrigin,
    credentials: true,
    methods: ['GET', 'POST', 'PATCH', 'DELETE', 'OPTIONS'],
  })

  /* ----------------------------- OpenAPI ----------------------------- */
  if (config.enableSwagger) {
    await app.register(swagger, {
      openapi: {
        openapi: '3.0.3',
        info: {
          title: '企业级研发迭代规划智能 Agent · 开放 API',
          version: deps.version,
          description:
            '基于 Kahn 拓扑排序 + AOE 关键路径的迭代规划服务。' +
            '所有底层算法（DAG 环检测、关键路径、工时缓冲、风险检测）均按规范固定实现，不对外提供替换入口。',
        },
        tags: [
          { name: 'system', description: '系统与元数据' },
          { name: 'plan', description: '迭代计划（创建 / 查询 / 重规划）' },
          { name: 'task', description: '任务增删改' },
          { name: 'view', description: 'DAG / 关键路径 / 风险 / 燃尽 / 报表' },
          { name: 'audit', description: '全链路审计日志' },
        ],
      },
    })
    await app.register(swaggerUi, {
      routePrefix: `${config.basePath}/docs`,
      uiConfig: { docExpansion: 'list', deepLinking: true },
    })
  }

  /* --------------------------- 静态资源（SPA） --------------------------- */
  const staticEnabled = !!config.staticDir && existsSync(config.staticDir)
  if (staticEnabled && config.staticDir) {
    await app.register(fastifyStatic, { root: config.staticDir, prefix: '/' })
  }

  /* ---------------------------- WebSocket ---------------------------- */
  await app.register(websocket)
  app.get(config.wsPath, { websocket: true, schema: { hide: true } }, (socket) => {
    // 心跳保活：清理半开连接，避免连接泄漏
    const heartbeat = setInterval(() => {
      try {
        socket.ping()
      } catch {
        clearInterval(heartbeat)
      }
    }, 30_000)

    const send = (payload: unknown): void => {
      // 1 === WebSocket.OPEN
      if (socket.readyState === 1) socket.send(JSON.stringify(payload))
    }
    send({ type: 'connected', at: new Date().toISOString() })

    const unsubscribe = deps.events.subscribe((event) => send(event))
    const cleanup = (): void => {
      clearInterval(heartbeat)
      unsubscribe()
    }
    socket.on('close', cleanup)
    socket.on('error', cleanup)
  })

  /* ------------------------------ 鉴权 ------------------------------ */
  app.addHook('onRequest', async (request, reply) => {
    if (!config.apiToken) return
    const url = request.raw.url ?? ''
    // 静态资源、健康检查、Swagger UI 不参与鉴权（部署在内网 / 网关后）
    if (!url.startsWith(config.basePath)) return
    if (url.startsWith(`${config.basePath}/health`)) return
    if (url.startsWith(`${config.basePath}/docs`)) return

    const header = request.headers.authorization ?? ''
    const token = header.startsWith('Bearer ') ? header.slice('Bearer '.length) : ''
    if (token !== config.apiToken) {
      const error = toApiError(apiErrors.unauthorized())
      await reply.code(error.statusCode).send(toErrorBody(error))
    }
  })

  /* ------------------------------ 路由 ------------------------------ */
  registerApiRoutes(app, {
    config,
    manager: deps.manager,
    auditRepository: deps.bundle.audit,
    events: deps.events,
    storageKind: deps.bundle.kind,
    startedAt: Date.now(),
    version: deps.version,
  })

  /* --------------------------- 统一错误处理 --------------------------- */
  app.setErrorHandler((error, request, reply) => {
    const apiError = toApiError(error)
    const detail = {
      method: request.method,
      url: request.raw.url,
      code: apiError.code,
      statusCode: apiError.statusCode,
    }
    if (apiError.statusCode >= 500) {
      request.log.error({ ...detail, err: error }, '请求处理失败')
    } else {
      request.log.warn({ ...detail, message: apiError.message }, '请求被拒绝')
    }
    void reply.code(apiError.statusCode).send(toErrorBody(apiError))
  })

  /* -------------------------- 404 与 SPA 兜底 -------------------------- */
  app.setNotFoundHandler((request, reply) => {
    const url = request.raw.url ?? ''
    const isApi = url.startsWith(config.basePath)
    if (!isApi && request.method === 'GET' && staticEnabled) {
      // 前端为 history 模式路由，未匹配的静态路径回落到 index.html
      return reply.type('text/html; charset=utf-8').sendFile('index.html')
    }
    const error = apiErrors.notFound(`路由不存在：${request.method} ${url}`)
    return reply.code(error.statusCode).send(toErrorBody(error))
  })

  return app
}

/** 构建并监听端口的完整启动流程 */
export async function startServer(deps: BuildServerDeps): Promise<ServerHandle> {
  const app = await buildServer(deps)
  const { config } = deps
  try {
    await app.listen({ host: config.host, port: config.port })
  } catch (error) {
    // 端口占用 / 权限不足等启动失败必须显式抛错，杜绝“静默假启动”
    app.log.error({ err: error }, 'API 服务启动失败')
    throw error
  }
  const address = app.server.address()
  const printable = typeof address === 'object' && address ? `${address.address}:${address.port}` : String(address)
  app.log.info(`迭代规划 API 已启动：http://${printable}${config.basePath}（docs: ${config.basePath}/docs）`)
  return {
    app,
    address: printable,
    close: async () => {
      await app.close()
    },
  }
}
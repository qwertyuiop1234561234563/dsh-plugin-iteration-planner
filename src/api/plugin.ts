/**
 * Cordis 插件形态：在 DeepSeek Harness 内部托管迭代规划控制台 API。
 *
 * 约束（§7.1）：不改动 Harness / Cordis 内核源码。本插件只是
 *  - 复用内核提供的 `ctx.logger` 与 `ctx.llm`；
 *  - 通过 `ctx.effect` 注册可逆副作用（启动 HTTP 服务，插件卸载时自动关闭）。
 *
 * 挂载方式（cordis.yml）：
 * ```yaml
 * - name: ./src/api/plugin.ts
 *   config:
 *     port: 8080
 *     staticDir: ./web/dist
 *     llmProvider: deepseek
 *     llmModel: deepseek-chat
 * ```
 * 若仅需独立部署控制台，可改用 `npm run api:start`（见 standalone.ts）。
 */

import type { Context } from '@deepseek-ai/cordis'
import type { BufferRule } from '../constants/enums.js'
import type { PlannerExtensions } from '../extensions/interfaces.js'
import { createHarnessLlmClient } from '../llm/llm-decomposer.js'
import type { LlmClient } from '../llm/types.js'
import type { RuntimeLogger } from './audit-bridge.js'
import { loadApiConfig, readPackageVersion, type ApiConfig } from './config.js'
import { createApiRuntime, type ApiRuntime } from './runtime.js'
import { startServer, type ServerHandle } from './server.js'
import type { PlannerServiceOptions } from '../service/planner-service.js'

/** 插件配置：未提供的字段回退到环境变量（{@link loadApiConfig}） */
export interface ApiPluginConfig {
  host?: string
  port?: number
  basePath?: string
  corsOrigin?: string | string[]
  apiToken?: string
  storageDriver?: 'memory' | 'postgres'
  /** PostgreSQL 连接串（storageDriver=postgres 时必填） */
  databaseUrl?: string
  /** 前端构建产物目录（存在则托管 SPA） */
  staticDir?: string
  wsPath?: string
  logLevel?: string
  enableSwagger?: boolean
  fallbackToRule?: boolean
  workHoursPerDay?: number
  overloadThreshold?: number
  /** Harness LLM provider（例如 deepseek），与 llmModel 同时提供则启用 LLM 主路径 */
  llmProvider?: string
  llmModel?: string
  /** 缓冲规则覆盖（§3.3） */
  bufferRules?: readonly BufferRule[]
  /** 缓冲叠加顺序覆盖 */
  bufferApplyOrder?: readonly ('type' | 'priority')[]
  /** 外部系统扩展点（Jira / Git / CI-CD） */
  extensions?: PlannerExtensions
}

export const name = 'iteration-planner-api'

/** 本插件不依赖其他服务，仅托管 HTTP 服务 */
export const inject: string[] = []

/** 合并插件配置到环境配置之上（仅覆盖显式提供的字段） */
function mergeConfig(pluginConfig: ApiPluginConfig): ApiConfig {
  const base = loadApiConfig()
  const defined = Object.fromEntries(
    Object.entries(pluginConfig).filter(([, value]) => value !== undefined),
  ) as Partial<ApiPluginConfig>
  const storageDriver = defined.storageDriver ?? base.storageDriver
  return {
    ...base,
    ...(defined.host !== undefined ? { host: defined.host } : {}),
    ...(defined.port !== undefined ? { port: defined.port } : {}),
    ...(defined.basePath !== undefined ? { basePath: defined.basePath } : {}),
    ...(defined.corsOrigin !== undefined ? { corsOrigin: defined.corsOrigin } : {}),
    ...(defined.apiToken !== undefined ? { apiToken: defined.apiToken } : {}),
    ...(defined.staticDir !== undefined ? { staticDir: defined.staticDir } : {}),
    ...(defined.wsPath !== undefined ? { wsPath: defined.wsPath } : {}),
    ...(defined.logLevel !== undefined ? { logLevel: defined.logLevel } : {}),
    ...(defined.enableSwagger !== undefined ? { enableSwagger: defined.enableSwagger } : {}),
    ...(defined.fallbackToRule !== undefined ? { fallbackToRule: defined.fallbackToRule } : {}),
    ...(defined.workHoursPerDay !== undefined ? { workHoursPerDay: defined.workHoursPerDay } : {}),
    ...(defined.overloadThreshold !== undefined ? { overloadThreshold: defined.overloadThreshold } : {}),
    storageDriver,
    postgres: defined.databaseUrl ? { connectionString: defined.databaseUrl } : base.postgres,
  }
}

/** 由 Harness 日志服务构造运行日志（失败时回退标准错误） */
function createHarnessLogger(ctx: Context): RuntimeLogger {
  const write = (level: 'info' | 'error', message: string): void => {
    try {
      if (level === 'error') ctx.logger.error(message)
      else ctx.logger.info(message)
    } catch {
      process.stderr.write(`${message}\n`)
    }
  }
  return {
    info: (message) => write('info', message),
    error: (message) => write('error', message),
  }
}

export function apply(ctx: Context, pluginConfig: ApiPluginConfig = {}): void {
  const config = mergeConfig(pluginConfig)
  const logger = createHarnessLogger(ctx)

  // LLM 主路径：Harness 内部优先复用 ctx.llm
  const llm: LlmClient | undefined =
    pluginConfig.llmProvider && pluginConfig.llmModel
      ? createHarnessLlmClient(ctx, { provider: pluginConfig.llmProvider, model: pluginConfig.llmModel })
      : undefined

  const serviceOptions: Partial<Omit<PlannerServiceOptions, 'audit'>> = {
    ...(pluginConfig.bufferRules ? { bufferRules: pluginConfig.bufferRules } : {}),
    ...(pluginConfig.bufferApplyOrder ? { bufferApplyOrder: pluginConfig.bufferApplyOrder } : {}),
    ...(pluginConfig.extensions ? { extensions: pluginConfig.extensions } : {}),
  }

  // 已启动的运行时句柄；供卸载时按顺序释放
  let started: { runtime: ApiRuntime; handle: ServerHandle } | undefined

  // 通过 ctx.effect 注册可逆副作用：插件卸载时自动关闭 HTTP 服务并释放仓储
  ctx.effect(() => {
    const booting = (async (): Promise<void> => {
      const runtime = await createApiRuntime({
        config,
        version: readPackageVersion(),
        llm,
        logger,
        serviceOptions,
      })
      const handle = await startServer({
        config: runtime.config,
        bundle: runtime.bundle,
        manager: runtime.manager,
        events: runtime.events,
        version: runtime.version,
      })
      started = { runtime, handle }
    })()
      // 启动失败必须显式记录，绝不静默
      .catch((error: unknown) => {
        logger.error(
          `[iteration-planner/api] 插件托管启动失败：${error instanceof Error ? `${error.name}: ${error.message}` : String(error)}`,
        )
      })

    return async (): Promise<void> => {
      await booting
      if (!started) return
      await started.handle.close()
      await started.runtime.close()
      started = undefined
    }
  }, 'iteration-planner-api:http-server')
}
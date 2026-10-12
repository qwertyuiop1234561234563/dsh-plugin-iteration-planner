/**
 * API 服务配置：从环境变量加载，集中管理，避免魔法值散落。
 *
 * 该配置同时服务于两种启动方式：
 *  - 独立启动（`npm run api:start` / Docker 容器）；
 *  - 作为 Cordis 插件挂载（由 Harness 传入 config，未传字段回退到环境变量）。
 */

import { readFileSync } from 'node:fs'
import type { StorageDriver } from '../repository/index.js'
import type { PostgresOptions } from '../repository/postgres.js'

export interface ApiConfig {
  /** 监听地址，默认 0.0.0.0 */
  host: string
  /** 监听端口，默认 8080 */
  port: number
  /** REST 路由前缀，默认 /api */
  basePath: string
  /** CORS 允许来源；'*' 表示全部放开 */
  corsOrigin: string | string[]
  /** 可选 Bearer Token；配置后所有 /api 写操作与查询均需鉴权 */
  apiToken?: string
  /** 存储驱动 */
  storageDriver: StorageDriver
  /** PostgreSQL 连接参数（storageDriver=postgres 时生效） */
  postgres: PostgresOptions
  /** 前端静态资源目录（存在则托管）；为空则仅提供 API */
  staticDir?: string
  /** WebSocket 事件推送路径 */
  wsPath: string
  /** 日志级别 */
  logLevel: string
  /** 是否启用 Swagger UI */
  enableSwagger: boolean
  /** LLM 失败时是否降级为规则实现 */
  fallbackToRule: boolean
  /** 单开发人单日工时 */
  workHoursPerDay: number
  /** 人员过载阈值（小时） */
  overloadThreshold: number
  /** OpenAI 兼容 LLM 网关（独立启动时启用 LLM 主路径，可选） */
  llm?: { baseUrl: string; apiKey: string; model: string }
}

/** 读取布尔型环境变量 */
function envBool(value: string | undefined, fallback: boolean): boolean {
  if (value === undefined || value === '') return fallback
  return !['0', 'false', 'no', 'off'].includes(value.toLowerCase())
}

/** 读取数字型环境变量 */
function envNumber(value: string | undefined, fallback: number): number {
  if (value === undefined || value === '') return fallback
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : fallback
}

/** 从环境变量构造 API 配置 */
export function loadApiConfig(env: NodeJS.ProcessEnv = process.env): ApiConfig {
  const corsOrigin = env.API_CORS_ORIGIN ?? '*'
  const storageDriver: StorageDriver = env.STORAGE_DRIVER === 'postgres' ? 'postgres' : 'memory'

  const postgres: PostgresOptions = env.DATABASE_URL
    ? { connectionString: env.DATABASE_URL }
    : {
        host: env.PGHOST ?? 'localhost',
        port: envNumber(env.PGPORT, 5432),
        user: env.PGUSER ?? 'postgres',
        password: env.PGPASSWORD ?? 'postgres',
        database: env.PGDATABASE ?? 'iteration_planner',
      }

  const llmBaseUrl = env.LLM_BASE_URL
  const llmApiKey = env.LLM_API_KEY
  const llmModel = env.LLM_MODEL

  return {
    host: env.API_HOST ?? '0.0.0.0',
    port: envNumber(env.API_PORT, 8080),
    basePath: env.API_BASE_PATH ?? '/api',
    corsOrigin: corsOrigin === '*' ? '*' : corsOrigin.split(',').map((s) => s.trim()).filter(Boolean),
    apiToken: env.API_TOKEN && env.API_TOKEN.length > 0 ? env.API_TOKEN : undefined,
    storageDriver,
    postgres,
    staticDir: env.WEB_STATIC_DIR && env.WEB_STATIC_DIR.length > 0 ? env.WEB_STATIC_DIR : undefined,
    wsPath: env.API_WS_PATH ?? '/ws',
    logLevel: env.LOG_LEVEL ?? 'info',
    enableSwagger: envBool(env.API_ENABLE_SWAGGER, true),
    fallbackToRule: envBool(env.PLANNER_FALLBACK_TO_RULE, true),
    workHoursPerDay: envNumber(env.PLANNER_WORK_HOURS_PER_DAY, 8),
    overloadThreshold: envNumber(env.PLANNER_OVERLOAD_THRESHOLD, 8),
    llm:
      llmBaseUrl && llmModel
        ? { baseUrl: llmBaseUrl, apiKey: llmApiKey ?? '', model: llmModel }
        : undefined,
  }
}

/** 读取包版本号（健康检查 / OpenAPI 文档展示） */
export function readPackageVersion(): string {
  try {
    const raw = readFileSync(new URL('../../package.json', import.meta.url), 'utf8')
    const pkg = JSON.parse(raw) as { version?: string }
    return pkg.version ?? '0.0.0'
  } catch {
    return '0.0.0'
  }
}
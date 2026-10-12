/**
 * API 运行时装配：把「配置 + 仓储 + 事件总线 + 会话管理器」组装为一个可关闭的运行时对象。
 *
 * 独立进程（standalone）与 Cordis 插件（plugin）共用本函数，保证两条启动路径行为完全一致。
 */

import type { LlmClient } from '../llm/types.js'
import { AuditLogger } from '../logger/audit.js'
import { createRepository, type RepositoryBundle } from '../repository/index.js'
import type { ApiConfig } from './config.js'
import { createPersistingAuditSink, createStdLogger, type RuntimeLogger } from './audit-bridge.js'
import { PlanEventBus } from './events.js'
import { PlanSessionManager } from './plan-session.js'
import type { PlannerServiceOptions } from '../service/planner-service.js'

/** 运行时构造参数 */
export interface ApiRuntimeOptions {
  config: ApiConfig
  /** 应用版本（写入健康检查与 OpenAPI） */
  version: string
  /** 可选 LLM 客户端（Harness 模式由 ctx.llm 适配，独立部署由 OpenAI 兼容网关构造） */
  llm?: LlmClient
  /** 运行日志 */
  logger?: RuntimeLogger
  /** 追加的服务层配置（缓冲规则覆盖、外部系统扩展点等） */
  serviceOptions?: Partial<Omit<PlannerServiceOptions, 'audit'>>
}

/** API 运行时句柄 */
export interface ApiRuntime {
  config: ApiConfig
  bundle: RepositoryBundle
  events: PlanEventBus
  manager: PlanSessionManager
  version: string
  /** 释放仓储与会话资源 */
  close(): Promise<void>
}

/**
 * 创建 API 运行时。
 * 数据库不可达 / 迁移失败等初始化问题会明确抛出，由调用方决定退出或提示。
 */
export async function createApiRuntime(options: ApiRuntimeOptions): Promise<ApiRuntime> {
  const { config } = options
  const logger = options.logger ?? createStdLogger()

  // 1) 仓储：按驱动建表 / 迁移（失败即抛出，绝不静默降级）
  const bundle = await createRepository({
    driver: config.storageDriver,
    postgres: config.postgres,
  })

  // 2) 事件总线（WebSocket 推送）
  const events = new PlanEventBus()

  // 3) 服务层配置：LLM 与阈值来自配置，其余可被调用方覆盖
  const serviceOptions: Omit<PlannerServiceOptions, 'audit'> = {
    llm: options.llm,
    fallbackToRule: config.fallbackToRule,
    workHoursPerDay: config.workHoursPerDay,
    overloadThreshold: config.overloadThreshold,
    ...options.serviceOptions,
  }

  // 4) 审计器工厂：每个计划一个绑定了 planId 的审计器，落库 + 转发运行日志
  const auditFor = (planId: string): AuditLogger =>
    new AuditLogger(createPersistingAuditSink({ repository: bundle.audit, planId, logger }))

  // 5) 会话管理器
  const manager = new PlanSessionManager({
    planRepository: bundle.plan,
    events,
    auditFor,
    serviceOptions,
  })

  // 6) 启动审计记录（全局事件，无 planId）
  const bootAudit = new AuditLogger(createPersistingAuditSink({ repository: bundle.audit, logger }))
  bootAudit.record({
    action: 'api.startup',
    ok: true,
    durationMs: 0,
    output: {
      version: options.version,
      storage: bundle.kind,
      basePath: config.basePath,
      llm: options.llm?.name ?? 'rule-based',
    },
  })

  return {
    config,
    bundle,
    events,
    manager,
    version: options.version,
    async close(): Promise<void> {
      manager.dispose()
      await bundle.close()
    },
  }
}
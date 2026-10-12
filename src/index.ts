/**
 * 插件入口（Cordis 插件）：企业级研发迭代规划智能 Agent。
 *
 * 约束（§7.1）：不改动 Harness / Cordis 内核源码，全部业务逻辑封装在本插件内。
 * 分层：models（实体） / constants（枚举） / algorithm（Kahn + AOE） /
 *       service（业务） / llm（策略） / tools（工具） / prompt（编排） / logger（审计）。
 *
 * 插件形态：导出 `name` / `inject` / `apply`，由 Harness 的 Loader 挂载。
 */

import type { Context } from '@deepseek-ai/cordis'
import type { BufferRule } from './constants/enums.js'
import type { PlannerExtensions } from './extensions/interfaces.js'
import { createHarnessLlmClient } from './llm/llm-decomposer.js'
import type { LlmClient } from './llm/types.js'
import { AuditLogger } from './logger/audit.js'
import { contributeSystemPrompt, ITERATION_PLANNER_SYSTEM_PROMPT } from './prompt/system-prompt.js'
import { PlannerService } from './service/planner-service.js'
import { registerAllTools } from './tools/index.js'

/** 插件配置（在 cordis.yml 中通过 config 传入） */
export interface PluginConfig {
  /** 自定义 LLM 客户端；提供后启用 LLM 主路径 */
  llm?: LlmClient
  /** Harness LLM provider（配合 ctx.llm 使用），例如 'deepseek' */
  llmProvider?: string
  /** Harness LLM model，例如 'deepseek-chat' */
  llmModel?: string
  /** LLM 失败时是否降级到规则实现，默认 true */
  fallbackToRule?: boolean
  /** 单日工时，默认 8 */
  workHoursPerDay?: number
  /** 人员过载阈值（小时），默认等于单日工时 */
  overloadThreshold?: number
  /** 缓冲规则覆盖（§3.3） */
  bufferRules?: readonly BufferRule[]
  /** 缓冲叠加顺序覆盖 */
  bufferApplyOrder?: readonly ('type' | 'priority')[]
  /** 外部系统扩展点（Jira / Git / CI-CD），当前仅保留接口 */
  extensions?: PlannerExtensions
  /** 自定义审计输出（默认走 ctx.logger） */
  auditSink?: (line: string) => void
  /** 自定义角色提示词（默认使用内置 §六 角色设定） */
  systemPrompt?: string
}

export const name = 'iteration-planner'

/** 声明依赖：等待工具注册表就绪后再注册工具 */
export const inject = ['tools']

export function apply(ctx: Context, config: PluginConfig = {}): void {
  // 1) 审计日志：默认输出到 Harness 日志，便于与轨迹视图对齐
  const sink =
    config.auditSink ??
    ((line: string) => {
      try {
        ctx.logger.info(line)
      } catch {
        process.stderr.write(`${line}\n`)
      }
    })
  const audit = new AuditLogger(sink)

  // 2) 组装 LLM 客户端：显式 client 优先，其次由 ctx.llm 适配；都没有则全程规则实现
  const llm: LlmClient | undefined =
    config.llm ??
    (config.llmProvider && config.llmModel
      ? createHarnessLlmClient(ctx, { provider: config.llmProvider, model: config.llmModel })
      : undefined)

  // 3) 创建业务服务
  const service = new PlannerService({
    audit,
    llm,
    fallbackToRule: config.fallbackToRule,
    workHoursPerDay: config.workHoursPerDay,
    overloadThreshold: config.overloadThreshold,
    bufferRules: config.bufferRules,
    bufferApplyOrder: config.bufferApplyOrder,
    extensions: config.extensions,
  })

  // 4) 注册全部工具（10 个规范工具 + plan_iteration）
  registerAllTools(ctx, service)

  // 5) 尽力注入角色提示词
  const prompt = config.systemPrompt ?? ITERATION_PLANNER_SYSTEM_PROMPT
  const injected = contributeSystemPrompt(ctx, prompt)
  audit.record({
    action: 'plugin.load',
    ok: true,
    durationMs: 0,
    output: {
      decomposer: service.getDecomposerKind(),
      systemPromptInjected: injected,
      toolCount: 11,
    },
  })
}

export { PlannerService } from './service/planner-service.js'
export { ITERATION_PLANNER_SYSTEM_PROMPT } from './prompt/system-prompt.js'
export * from './algorithm/index.js'
export * from './models/entities.js'
export * from './constants/enums.js'
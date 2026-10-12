/**
 * 独立启动入口（Docker / 裸机部署）。
 *
 * 运行方式：
 *   npm run api:start            # 读取 .env + 环境变量
 *   node --import tsx src/api/standalone.ts
 *
 * 该入口不依赖 DeepSeek Harness 运行时，可与现有 Cordis 插件并行部署，
 * 也可单独作为「迭代规划开放服务」对外提供 REST + WebSocket 能力。
 */

import { createStdLogger } from './audit-bridge.js'
import { loadApiConfig, readPackageVersion } from './config.js'
import { createOpenAiCompatibleClient } from './llm-client.js'
import { createApiRuntime } from './runtime.js'
import { startServer } from './server.js'

/** 尽力加载 .env（Node 20.12+ 内置，缺失文件时静默跳过） */
function loadDotEnv(): void {
  try {
    process.loadEnvFile?.()
  } catch {
    // 无 .env 文件属于正常情况，仅依赖进程环境变量
  }
}

async function main(): Promise<void> {
  loadDotEnv()
  const config = loadApiConfig()
  const logger = createStdLogger()

  // 配置了 OpenAI 兼容网关则启用 LLM 主路径，否则使用规则实现（离线可复现）
  const llm = config.llm ? createOpenAiCompatibleClient(config.llm) : undefined

  const runtime = await createApiRuntime({ config, version: readPackageVersion(), llm, logger })
  const handle = await startServer({
    config: runtime.config,
    bundle: runtime.bundle,
    manager: runtime.manager,
    events: runtime.events,
    version: runtime.version,
  })

  logger.info(
    `[iteration-planner/api] 已启动，storage=${runtime.bundle.kind}，llm=${llm ? llm.name : 'rule-based'}`,
  )

  // 优雅退出：先停 HTTP（拒绝新请求），再释放仓储连接池
  let closing = false
  const shutdown = async (signal: string): Promise<void> => {
    if (closing) return
    closing = true
    logger.info(`[iteration-planner/api] 收到 ${signal}，开始优雅退出……`)
    try {
      await handle.close()
      await runtime.close()
      logger.info('[iteration-planner/api] 已安全退出')
      process.exitCode = 0
    } catch (error) {
      logger.error(
        `[iteration-planner/api] 退出过程中发生异常：${error instanceof Error ? error.message : String(error)}`,
      )
      process.exitCode = 1
    }
  }

  process.on('SIGINT', () => void shutdown('SIGINT'))
  process.on('SIGTERM', () => void shutdown('SIGTERM'))
}

main().catch((error: unknown) => {
  // 启动失败必须显式可见（端口占用、数据库不可达等）
  process.stderr.write(
    `\n[iteration-planner/api] 启动失败：${error instanceof Error ? `${error.name}: ${error.message}` : String(error)}\n`,
  )
  process.exitCode = 1
})
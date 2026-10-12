/**
 * API 层统一出口。
 *
 * 供三类调用方使用：
 *  - 独立进程入口 `standalone.ts`（`npm run api:start`）；
 *  - Cordis 插件入口 `plugin.ts`（在 Harness 内挂载）；
 *  - 测试：`buildServer()` + `fastify.inject()` 做无网络集成测试。
 */

export { buildServer, startServer, type BuildServerDeps, type ServerHandle } from './server.js'
export { createApiRuntime, type ApiRuntime, type ApiRuntimeOptions } from './runtime.js'
export { loadApiConfig, readPackageVersion, type ApiConfig } from './config.js'
export { createOpenAiCompatibleClient, type OpenAiCompatibleOptions } from './llm-client.js'
export { createPersistingAuditSink, createStdLogger, type RuntimeLogger } from './audit-bridge.js'
export { PlanEventBus, type PlanEvent, type PlanEventType } from './events.js'
export { PlanSession, PlanSessionManager, type PlanSessionDeps } from './plan-session.js'
export { buildGraphPayload, type GraphEdge, type GraphNode, type GraphPayload } from './graph.js'
export { ApiError, apiErrors, toApiError, toErrorBody, type ApiErrorBody } from './errors.js'
export * from './validation.js'
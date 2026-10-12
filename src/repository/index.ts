/**
 * 仓储层统一出口与工厂。
 *
 * 通过 `STORAGE_DRIVER` 选择实现：
 *  - `memory`（默认）：零依赖，适合本地开发 / 单测 / Demo；
 *  - `postgres`：生产落地，使用 PostgreSQL 持久化。
 */

import { createMemoryRepository } from './memory.js'
import { createPostgresRepository, type PostgresOptions } from './postgres.js'
import type { RepositoryBundle } from './types.js'

export * from './types.js'
export { MemoryPlanRepository, MemoryAuditRepository, createMemoryRepository } from './memory.js'
export {
  PostgresPlanRepository,
  PostgresAuditRepository,
  createPostgresRepository,
  runMigrations,
  RepositoryError,
  type PostgresOptions,
} from './postgres.js'
export { MIGRATIONS, type Migration } from './migrations.js'

/** 存储驱动类型 */
export type StorageDriver = 'memory' | 'postgres'

/** 仓储工厂配置 */
export interface RepositoryFactoryOptions {
  driver: StorageDriver
  /** postgres 驱动所需连接参数 */
  postgres?: PostgresOptions
}

/**
 * 创建仓储聚合并按需执行初始化迁移。
 * 创建失败（例如数据库不可达）会抛出 {@link RepositoryError}，由上层给出明确提示。
 */
export async function createRepository(options: RepositoryFactoryOptions): Promise<RepositoryBundle> {
  const bundle: RepositoryBundle =
    options.driver === 'postgres'
      ? createPostgresRepository(options.postgres ?? {})
      : createMemoryRepository()
  await bundle.plan.init()
  await bundle.audit.init()
  return bundle
}
/**
 * PostgreSQL 仓储实现（生产落地形态）。
 *
 * 特点：
 *  - 直接使用 `pg` 连接池 + 手写 SQL，不引入 ORM，SQL 全部可审计；
 *  - 启动时执行 {@link MIGRATIONS} 幂等迁移；
 *  - 计划以 JSONB 全量快照写入，同时冗余摘要列支撑高效列表查询；
 *  - 所有数据库异常统一包装为 {@link RepositoryError} 并向上抛出（§7.8 不静默崩溃）。
 */

import { randomUUID } from 'node:crypto'
import { Pool, type PoolConfig, type PoolClient } from 'pg'
import type { ProjectPlan } from '../models/entities.js'
import { MIGRATIONS } from './migrations.js'
import { toPlanSummary, type AuditRecord, type AuditRepository, type PlanRepository, type PlanSummary, type RepositoryBundle } from './types.js'

/** 仓储层统一异常：携带可读信息，便于 API 层映射为 5xx 并输出明确提示 */
export class RepositoryError extends Error {
  constructor(message: string, readonly cause?: unknown) {
    super(message)
    this.name = 'RepositoryError'
  }
}

/** PostgreSQL 连接配置 */
export interface PostgresOptions {
  /** 连接串（优先），例如 postgres://user:pass@host:5432/db */
  connectionString?: string
  host?: string
  port?: number
  user?: string
  password?: string
  database?: string
  /** 连接池上限，默认 10 */
  max?: number
}

/** 由环境风格配置构造连接池配置 */
function toPoolConfig(options: PostgresOptions): PoolConfig {
  if (options.connectionString) return { connectionString: options.connectionString, max: options.max ?? 10 }
  return {
    host: options.host,
    port: options.port,
    user: options.user,
    password: options.password,
    database: options.database,
    max: options.max ?? 10,
  }
}

/** PostgreSQL 计划仓储 */
export class PostgresPlanRepository implements PlanRepository {
  constructor(private readonly pool: Pool) {}

  async init(): Promise<void> {
    await runMigrations(this.pool)
  }

  async save(plan: ProjectPlan): Promise<void> {
    const now = Date.now()
    const summary = toPlanSummary(plan, now, now)
    try {
      await this.pool.query(
        `INSERT INTO iteration_plan (
           plan_id, raw_requirement, iteration_deadline, iteration_start_date, dev_member_count,
           task_count, risk_count, high_risk_count, total_estimated_hours, total_duration_hours,
           payload, created_at, updated_at
         ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
         ON CONFLICT (plan_id) DO UPDATE SET
           raw_requirement       = EXCLUDED.raw_requirement,
           iteration_deadline    = EXCLUDED.iteration_deadline,
           iteration_start_date  = EXCLUDED.iteration_start_date,
           dev_member_count      = EXCLUDED.dev_member_count,
           task_count            = EXCLUDED.task_count,
           risk_count            = EXCLUDED.risk_count,
           high_risk_count       = EXCLUDED.high_risk_count,
           total_estimated_hours = EXCLUDED.total_estimated_hours,
           total_duration_hours  = EXCLUDED.total_duration_hours,
           payload               = EXCLUDED.payload,
           updated_at            = EXCLUDED.updated_at`,
        [
          plan.planId,
          plan.rawRequirement,
          plan.iterationDeadline,
          plan.iterationStartDate,
          plan.devMemberCount,
          summary.taskCount,
          summary.riskCount,
          summary.highRiskCount,
          summary.totalEstimatedHours,
          summary.totalDurationHours,
          JSON.stringify(plan),
          now,
          now,
        ],
      )
    } catch (error) {
      throw new RepositoryError(`保存计划失败（planId=${plan.planId}）：${asMessage(error)}`, error)
    }
  }

  async get(planId: string): Promise<ProjectPlan | undefined> {
    try {
      const { rows } = await this.pool.query<{ payload: ProjectPlan }>(
        'SELECT payload FROM iteration_plan WHERE plan_id = $1',
        [planId],
      )
      return rows[0]?.payload
    } catch (error) {
      throw new RepositoryError(`读取计划失败（planId=${planId}）：${asMessage(error)}`, error)
    }
  }

  async list(): Promise<PlanSummary[]> {
    try {
      const { rows } = await this.pool.query<{
        plan_id: string
        raw_requirement: string
        iteration_deadline: string
        iteration_start_date: string
        dev_member_count: number
        task_count: number
        risk_count: number
        high_risk_count: number
        total_estimated_hours: string
        total_duration_hours: string
        created_at: string
        updated_at: string
      }>(
        `SELECT plan_id, raw_requirement, iteration_deadline, iteration_start_date, dev_member_count,
                task_count, risk_count, high_risk_count, total_estimated_hours, total_duration_hours,
                created_at, updated_at
           FROM iteration_plan
          ORDER BY updated_at DESC`,
      )
      return rows.map((r) => ({
        planId: r.plan_id,
        rawRequirement: r.raw_requirement,
        iterationDeadline: r.iteration_deadline,
        iterationStartDate: r.iteration_start_date,
        devMemberCount: Number(r.dev_member_count),
        taskCount: Number(r.task_count),
        riskCount: Number(r.risk_count),
        highRiskCount: Number(r.high_risk_count),
        totalEstimatedHours: Number(r.total_estimated_hours),
        totalDurationHours: Number(r.total_duration_hours),
        createdAt: Number(r.created_at),
        updatedAt: Number(r.updated_at),
      }))
    } catch (error) {
      throw new RepositoryError(`查询计划列表失败：${asMessage(error)}`, error)
    }
  }

  async remove(planId: string): Promise<boolean> {
    try {
      const result = await this.pool.query('DELETE FROM iteration_plan WHERE plan_id = $1', [planId])
      return (result.rowCount ?? 0) > 0
    } catch (error) {
      throw new RepositoryError(`删除计划失败（planId=${planId}）：${asMessage(error)}`, error)
    }
  }

  async close(): Promise<void> {
    await this.pool.end()
  }
}

/** PostgreSQL 审计仓储 */
export class PostgresAuditRepository implements AuditRepository {
  constructor(private readonly pool: Pool) {}

  async init(): Promise<void> {
    await runMigrations(this.pool)
  }

  async append(record: AuditRecord): Promise<void> {
    try {
      await this.pool.query(
        `INSERT INTO iteration_audit (record_id, plan_id, ts, action, ok, duration_ms, input, output, error)
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)`,
        [
          record.recordId,
          record.planId ?? null,
          record.ts,
          record.action,
          record.ok,
          Math.round(record.durationMs),
          record.input === undefined ? null : JSON.stringify(record.input),
          record.output === undefined ? null : JSON.stringify(record.output),
          record.error ?? null,
        ],
      )
    } catch (error) {
      throw new RepositoryError(`写入审计日志失败（action=${record.action}）：${asMessage(error)}`, error)
    }
  }

  async list(options: { planId?: string; limit?: number } = {}): Promise<AuditRecord[]> {
    const { planId, limit = 200 } = options
    try {
      const { rows } = await this.pool.query<{
        record_id: string
        plan_id: string | null
        ts: Date
        action: string
        ok: boolean
        duration_ms: number
        input: unknown
        output: unknown
        error: string | null
      }>(
        `SELECT record_id, plan_id, ts, action, ok, duration_ms, input, output, error
           FROM iteration_audit
          WHERE ($1::text IS NULL OR plan_id = $1)
          ORDER BY ts DESC
          LIMIT $2`,
        [planId ?? null, limit],
      )
      return rows.map((r) => ({
        recordId: r.record_id,
        planId: r.plan_id ?? undefined,
        ts: r.ts instanceof Date ? r.ts.toISOString() : String(r.ts),
        action: r.action,
        ok: r.ok,
        durationMs: Number(r.duration_ms),
        input: r.input ?? undefined,
        output: r.output ?? undefined,
        error: r.error ?? undefined,
      }))
    } catch (error) {
      throw new RepositoryError(`查询审计日志失败：${asMessage(error)}`, error)
    }
  }

  async close(): Promise<void> {
    await this.pool.end()
  }
}

/* -------------------------------------------------------------------------- */
/*                                  内部工具                                    */
/* -------------------------------------------------------------------------- */

function asMessage(error: unknown): string {
  return error instanceof Error ? `${error.name}: ${error.message}` : String(error)
}

/**
 * 幂等执行迁移：
 *  - 读取 schema_migration 中已应用的 ID；
 *  - 逐个在事务中执行未应用迁移并登记。
 */
export async function runMigrations(pool: Pool): Promise<void> {
  const client: PoolClient = await pool.connect()
  try {
    // 迁移记录表需先存在（与 001_init 中的 DDL 保持一致）
    await client.query(
      'CREATE TABLE IF NOT EXISTS schema_migration (id TEXT PRIMARY KEY, applied_at BIGINT NOT NULL)',
    )
    const { rows } = await client.query<{ id: string }>('SELECT id FROM schema_migration')
    const applied = new Set(rows.map((r) => r.id))

    for (const migration of MIGRATIONS) {
      if (applied.has(migration.id)) continue
      await client.query('BEGIN')
      try {
        await client.query(migration.sql)
        await client.query('INSERT INTO schema_migration (id, applied_at) VALUES ($1, $2)', [
          migration.id,
          Date.now(),
        ])
        await client.query('COMMIT')
      } catch (error) {
        await client.query('ROLLBACK')
        throw new RepositoryError(`迁移执行失败（${migration.id}）：${asMessage(error)}`, error)
      }
    }
  } finally {
    client.release()
  }
}

/** 由环境风格配置构造 PostgreSQL 仓储聚合（共享同一连接池） */
export function createPostgresRepository(options: PostgresOptions): RepositoryBundle {
  const pool = new Pool(toPoolConfig(options))
  // 连接池级错误必须显式记录，避免进程静默挂掉
  pool.on('error', (error) => {
    process.stderr.write(`[iteration-planner/repository] PostgreSQL 连接池异常：${asMessage(error)}\n`)
  })
  const plan = new PostgresPlanRepository(pool)
  const audit = new PostgresAuditRepository(pool)
  return {
    plan,
    audit,
    kind: 'postgres',
    async close(): Promise<void> {
      await pool.end()
    },
  }
}

/** 生成审计记录 ID */
export function newAuditRecordId(): string {
  return `AUD-${randomUUID().slice(0, 12)}`
}
/**
 * 内存仓储实现。
 *
 * 用途：
 *  - 单元测试 / API 集成测试（无需外部依赖、秒级启动、用例隔离）；
 *  - 本地 Demo 与前端联调（`STORAGE_DRIVER=memory`）。
 *
 * 语义与 PostgreSQL 实现保持一致（同一 {@link toPlanSummary} 口径），
 * 审计记录采用环形缓冲，避免长时间运行内存无界增长。
 */

import type { ProjectPlan } from '../models/entities.js'
import { toPlanSummary, type AuditRecord, type AuditRepository, type PlanRepository, type PlanSummary, type RepositoryBundle } from './types.js'

/** 计划快照 + 时间戳 */
interface StoredPlan {
  plan: ProjectPlan
  createdAt: number
  updatedAt: number
}

/** 内存计划仓储 */
export class MemoryPlanRepository implements PlanRepository {
  private readonly store = new Map<string, StoredPlan>()

  async init(): Promise<void> {
    // 内存实现无需建表，保留方法以对齐接口语义
  }

  async save(plan: ProjectPlan): Promise<void> {
    const now = Date.now()
    const existing = this.store.get(plan.planId)
    // 深拷贝，避免调用方后续改动影响已存快照
    this.store.set(plan.planId, {
      plan: structuredClone(plan),
      createdAt: existing?.createdAt ?? now,
      updatedAt: now,
    })
  }

  async get(planId: string): Promise<ProjectPlan | undefined> {
    const hit = this.store.get(planId)
    return hit ? structuredClone(hit.plan) : undefined
  }

  async list(): Promise<PlanSummary[]> {
    return [...this.store.values()]
      .map((entry) => toPlanSummary(entry.plan, entry.createdAt, entry.updatedAt))
      .sort((a, b) => b.updatedAt - a.updatedAt)
  }

  async remove(planId: string): Promise<boolean> {
    return this.store.delete(planId)
  }

  async close(): Promise<void> {
    this.store.clear()
  }
}

/** 内存审计仓储（环形缓冲） */
export class MemoryAuditRepository implements AuditRepository {
  private readonly records: AuditRecord[] = []

  constructor(private readonly capacity = 2000) {}

  async init(): Promise<void> {
    // 无需建表
  }

  async append(record: AuditRecord): Promise<void> {
    this.records.push(record)
    if (this.records.length > this.capacity) {
      this.records.splice(0, this.records.length - this.capacity)
    }
  }

  async list(options: { planId?: string; limit?: number } = {}): Promise<AuditRecord[]> {
    const { planId, limit = 200 } = options
    const filtered = planId ? this.records.filter((r) => r.planId === planId) : [...this.records]
    return filtered.slice(-limit).reverse()
  }

  async close(): Promise<void> {
    this.records.length = 0
  }
}

/** 构造内存仓储聚合 */
export function createMemoryRepository(): RepositoryBundle {
  const plan = new MemoryPlanRepository()
  const audit = new MemoryAuditRepository()
  return {
    plan,
    audit,
    kind: 'memory',
    async close(): Promise<void> {
      await plan.close()
      await audit.close()
    },
  }
}
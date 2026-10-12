/**
 * 仓储层接口定义（持久化抽象）。
 *
 * 设计目标（§7.6 预留扩展 & 企业级落地）：
 *  - API / 前端只是「新增交付层」，业务算法与服务层保持不变；
 *  - 持久化通过接口注入，提供 PostgreSQL 生产实现与内存实现（单测 / 本地秒起）；
 *  - 后续可平滑替换为 MySQL / 达梦等其他存储，只需实现同一组接口。
 *
 * 存储模型选择：项目计划是一份「自洽的文档型快照」（任务 + 风险 + 关键路径 + 燃尽），
 * 因此采用「JSONB 全量载荷 + 冗余摘要列」的方式落库，既可完整还原，又能高效列表查询。
 */

import type { AuditEntry } from '../logger/audit.js'
import type { ProjectPlan } from '../models/entities.js'

/* -------------------------------------------------------------------------- */
/*                                  计划摘要                                    */
/* -------------------------------------------------------------------------- */

/** 计划列表项：轻量摘要，避免列表接口返回全量任务 */
export interface PlanSummary {
  planId: string
  rawRequirement: string
  iterationDeadline: string
  iterationStartDate: string
  devMemberCount: number
  taskCount: number
  riskCount: number
  highRiskCount: number
  totalEstimatedHours: number
  totalDurationHours: number
  /** 创建时间戳（毫秒） */
  createdAt: number
  /** 最近更新时间戳（毫秒） */
  updatedAt: number
}

/* -------------------------------------------------------------------------- */
/*                                  审计记录                                    */
/* -------------------------------------------------------------------------- */

/** 审计记录（持久化形态）：在 {@link AuditEntry} 基础上补充主键、计划归属与 ISO 时间 */
export interface AuditRecord extends AuditEntry {
  /** 审计记录唯一 ID */
  recordId: string
  /** 归属计划 ID（插件加载等全局事件可能为空） */
  planId?: string
  /** ISO 时间字符串 */
  ts: string
}

/* -------------------------------------------------------------------------- */
/*                                  仓储接口                                    */
/* -------------------------------------------------------------------------- */

/** 项目计划仓储 */
export interface PlanRepository {
  /** 初始化：执行迁移 / 建表（幂等） */
  init(): Promise<void>
  /** 覆盖保存一份完整计划快照（同 planId 幂等 upsert） */
  save(plan: ProjectPlan): Promise<void>
  /** 按 ID 读取完整计划 */
  get(planId: string): Promise<ProjectPlan | undefined>
  /** 列表查询（按更新时间倒序） */
  list(): Promise<PlanSummary[]>
  /** 删除计划，返回是否存在 */
  remove(planId: string): Promise<boolean>
  /** 释放资源（连接池等） */
  close(): Promise<void>
}

/** 审计日志仓储 */
export interface AuditRepository {
  /** 初始化（幂等） */
  init(): Promise<void>
  /** 追加一条审计记录 */
  append(record: AuditRecord): Promise<void>
  /** 查询审计记录（可按计划过滤，时间倒序） */
  list(options?: { planId?: string; limit?: number }): Promise<AuditRecord[]>
  /** 释放资源 */
  close(): Promise<void>
}

/** 仓储聚合：由工厂按配置产出（内存 / PostgreSQL） */
export interface RepositoryBundle {
  plan: PlanRepository
  audit: AuditRepository
  /** 存储类型标识，用于健康检查与日志 */
  readonly kind: 'memory' | 'postgres'
  /** 统一释放 */
  close(): Promise<void>
}

/** 从计划计算摘要（内存与 PG 实现共用，保证口径一致） */
export function toPlanSummary(plan: ProjectPlan, createdAt: number, updatedAt: number): PlanSummary {
  return {
    planId: plan.planId,
    rawRequirement: plan.rawRequirement,
    iterationDeadline: plan.iterationDeadline,
    iterationStartDate: plan.iterationStartDate,
    devMemberCount: plan.devMemberCount,
    taskCount: plan.taskList.length,
    riskCount: plan.riskList.length,
    highRiskCount: plan.riskList.filter((r) => r.level === 'HIGH').length,
    totalEstimatedHours: plan.totalEstimatedHours,
    totalDurationHours: plan.totalDurationHours,
    createdAt,
    updatedAt,
  }
}
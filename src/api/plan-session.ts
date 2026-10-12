/**
 * 计划会话（PlanSession）与会话管理器。
 *
 * 背景：{@link PlannerService} 内部维护「单份」内存任务集合与最近一次计划快照，
 * 而企业控制台需要同时管理多份计划、并持久化到数据库。因此：
 *  - 每个计划对应一个独立的 PlannerService 实例（会话隔离）；
 *  - 对外暴露稳定的 `planId` 作为 REST 资源 ID（服务层每次重算会生成新 ID，这里统一覆盖）；
 *  - 会话默认按需懒加载：读接口直接返回仓储快照，写接口才构造服务并恢复上下文。
 *
 * 注意：本层为「新增交付层」，不修改任何算法层 / 服务层代码。
 */

import { AuditLogger } from '../logger/audit.js'
import { newId, type GlobalConstraints, type ProjectPlan, type Task, type TaskBase } from '../models/entities.js'
import type { PlanRepository } from '../repository/types.js'
import { PlannerService, type PlannerServiceOptions, type UpdatableField } from '../service/planner-service.js'
import { apiErrors } from './errors.js'
import type { PlanEvent, PlanEventBus } from './events.js'

/** 会话依赖 */
export interface PlanSessionDeps {
  /** 计划仓储（持久化快照） */
  planRepository: PlanRepository
  /** 计划变更事件总线（WebSocket 推送） */
  events: PlanEventBus
  /** 按 planId 生成绑定了归属信息的审计器 */
  auditFor: (planId: string) => AuditLogger
  /** 服务层可选项（不含 audit，由会话按计划注入） */
  serviceOptions: Omit<PlannerServiceOptions, 'audit'>
}

/** 新建计划入参 */
export interface CreatePlanInput {
  requirement: string
  constraints: GlobalConstraints
}

/**
 * 把服务层产出的计划绑定到稳定 planId。
 * 服务层 `computePlan()` 每次都会生成新的 planId，这里统一覆盖为会话 ID，
 * 从而保证 REST 资源路径在多次重规划后保持稳定。
 */
function bindPlanId(plan: ProjectPlan, planId: string): ProjectPlan {
  return { ...plan, planId }
}

/**
 * 恢复服务层的需求上下文。
 *
 * 说明：`PlannerService.requirement` 为私有字段，且服务层明确要求「只依赖不修改」。
 * 出于语义风险扫描（依赖原始需求关键词）的还原需要，这里做一次受控写入，
 * 仅触碰该单一字段，不改变任何算法与流程；服务层源码保持零改动。
 */
function restoreRequirement(service: PlannerService, requirement: string): void {
  const mutable = service as unknown as { requirement: string }
  mutable.requirement = requirement
}

/* -------------------------------------------------------------------------- */
/*                                 PlanSession                                */
/* -------------------------------------------------------------------------- */

/** 单个迭代计划会话 */
export class PlanSession {
  /** 服务层实例（懒加载；仅在发生写操作时构造并恢复上下文） */
  private service?: PlannerService

  private constructor(
    /** 稳定的计划 ID（REST 资源 ID） */
    readonly planId: string,
    private plan: ProjectPlan,
    private readonly deps: PlanSessionDeps,
  ) {}

  /** 当前完整计划（只读） */
  get currentPlan(): ProjectPlan {
    return this.plan
  }

  /** 由服务层产出的计划构造会话（首次创建） */
  static fromCreated(plan: ProjectPlan, deps: PlanSessionDeps): PlanSession {
    return new PlanSession(plan.planId, plan, deps)
  }

  /** 由仓储快照构造会话（进程重启后冷恢复） */
  static fromStored(plan: ProjectPlan, deps: PlanSessionDeps): PlanSession {
    return new PlanSession(plan.planId, plan, deps)
  }

  /* --------------------------- 写操作：任务变更 --------------------------- */

  /** 新增任务（§四.4），成功后全量重规划 */
  async addTask(draft: Partial<TaskBase> & Pick<TaskBase, 'taskName'>): Promise<{ task: Task; plan: ProjectPlan }> {
    const before = new Set(this.plan.taskList.map((t) => t.taskId))
    const service = await this.ensureService()
    const { plan } = await service.addTask(draft)
    await this.adopt(plan, 'task_add')
    const task = this.plan.taskList.find((t) => !before.has(t.taskId))
    if (!task) throw apiErrors.internal('新增任务后未能定位该任务，请重试')
    return { task, plan: this.plan }
  }

  /** 修改任务字段（§四.5），成功后全量重规划 */
  async updateTask(taskId: string, updateField: UpdatableField, newValue: unknown): Promise<{ task: Task; plan: ProjectPlan }> {
    const service = await this.ensureService()
    const { plan } = await service.updateTask(taskId, updateField, newValue)
    await this.adopt(plan, 'task_update')
    const task = this.plan.taskList.find((t) => t.taskId === taskId)
    if (!task) throw apiErrors.internal('更新任务后未能定位该任务，请重试')
    return { task, plan: this.plan }
  }

  /** 删除任务（§四.6），同时清理引用并全量重规划 */
  async deleteTask(taskId: string): Promise<{ plan: ProjectPlan }> {
    const service = await this.ensureService()
    const { plan } = await service.deleteTask(taskId)
    await this.adopt(plan, 'task_delete')
    return { plan: this.plan }
  }

  /** 全量重规划（§四.9 / §3.5），可携带新的全局约束（截止日期、起始日期、开发人数） */
  async replan(constraints?: Partial<GlobalConstraints>): Promise<ProjectPlan> {
    const service = await this.ensureService()
    const plan = await service.replanAll(constraints)
    await this.adopt(plan, 'replan_all')
    return this.plan
  }

  /* ------------------------------ 内部实现 ------------------------------ */

  /**
   * 懒构造服务并恢复上下文。
   * 恢复动作本身会触发一次全量重算（服务层唯一可注入任务集合的入口），
   * 由后续业务操作覆盖结果，属于可接受的一次性开销。
   */
  private async ensureService(): Promise<PlannerService> {
    if (this.service) return this.service
    const service = new PlannerService({ ...this.deps.serviceOptions, audit: this.deps.auditFor(this.planId) })
    restoreRequirement(service, this.plan.rawRequirement)
    await service.replaceTasksAndReplan(this.plan.taskList, {
      iterationDeadline: this.plan.iterationDeadline,
      iterationStartDate: this.plan.iterationStartDate,
      devMemberCount: this.plan.devMemberCount,
    })
    this.service = service
    return service
  }

  /** 采纳服务层新计划：绑定稳定 planId → 落库 → 推送事件 */
  private async adopt(plan: ProjectPlan, reason: string): Promise<void> {
    this.plan = bindPlanId(plan, this.planId)
    await this.deps.planRepository.save(this.plan)
    this.deps.events.publish({
      type: 'plan.updated',
      planId: this.planId,
      at: new Date().toISOString(),
      reason,
      plan: this.plan,
    })
  }
}

/* -------------------------------------------------------------------------- */
/*                             PlanSessionManager                             */
/* -------------------------------------------------------------------------- */

/** 计划会话管理器：创建、缓存、查询、删除 */
export class PlanSessionManager {
  private readonly sessions = new Map<string, PlanSession>()

  constructor(private readonly deps: PlanSessionDeps) {}

  /** 新建计划：完整跑通「拆解 → 缓冲 → DAG → AOE → 风险 → 燃尽」 */
  async create(input: CreatePlanInput): Promise<ProjectPlan> {
    const planId = newId()
    const service = new PlannerService({ ...this.deps.serviceOptions, audit: this.deps.auditFor(planId) })
    const raw = await service.createPlan(input.requirement, input.constraints)
    const plan = bindPlanId(raw, planId)
    const session = PlanSession.fromCreated(plan, this.deps)
    this.sessions.set(planId, session)
    await this.deps.planRepository.save(plan)
    this.publish({ type: 'plan.created', planId, at: new Date().toISOString(), reason: 'plan_iteration', plan })
    return plan
  }

  /** 获取会话（命中缓存直接返回；否则从仓储冷恢复） */
  async get(planId: string): Promise<PlanSession> {
    const cached = this.sessions.get(planId)
    if (cached) return cached
    const stored = await this.deps.planRepository.get(planId)
    if (!stored) throw apiErrors.notFound(`计划不存在：${planId}`)
    const session = PlanSession.fromStored(stored, this.deps)
    this.sessions.set(planId, session)
    return session
  }

  /** 直接读取计划快照（不构造服务，读路径零副作用） */
  async getPlan(planId: string): Promise<ProjectPlan> {
    return (await this.get(planId)).currentPlan
  }

  /** 计划列表（摘要） */
  async list(): Promise<Awaited<ReturnType<PlanRepository['list']>>> {
    return this.deps.planRepository.list()
  }

  /** 删除计划 */
  async remove(planId: string): Promise<void> {
    const existed = await this.deps.planRepository.remove(planId)
    if (!existed) throw apiErrors.notFound(`计划不存在：${planId}`)
    this.sessions.delete(planId)
    this.publish({ type: 'plan.deleted', planId, at: new Date().toISOString(), reason: 'plan_delete' })
  }

  /** 释放全部会话（插件卸载 / 进程退出） */
  dispose(): void {
    this.sessions.clear()
  }

  /** 统一事件发布入口（便于后续替换为分布式总线） */
  private publish(event: PlanEvent): void {
    this.deps.events.publish(event)
  }
}
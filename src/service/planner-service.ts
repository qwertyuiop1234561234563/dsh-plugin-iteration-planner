/**
 * 业务服务层：迭代规划服务。
 *
 * 职责：
 *  - 维护内存中的任务集合与全局约束；
 *  - 编排「拆解 → DAG 校验 → 排期 → 风险扫描 → 燃尽 → 完整计划」流水线；
 *  - 处理任务新增/修改/删除，并触发全量重规划。
 *
 * 关键约束（§3.5）：重规划执行顺序严格固定，任何一步失败都抛出明确异常，绝不静默。
 */

import { buildDag, findDanglingPredecessors, kahnCheck, type DagCheckResult, type DagGraph } from '../algorithm/dag.js'
import { computeSchedule, type ScheduleResult } from '../algorithm/aoe.js'
import { buildBurndown, sumEstimatedHours } from '../algorithm/burndown.js'
import { todayISO } from '../algorithm/workday.js'
import {
  BUFFER_APPLY_ORDER,
  BUFFER_RULES,
  SCHEDULE,
  TaskStatus,
  type BufferRule,
  type Priority,
} from '../constants/enums.js'
import type { PlannerExtensions } from '../extensions/interfaces.js'
import type { AuditLogger } from '../logger/audit.js'
import { LlmDecomposer, LlmRiskScanner } from '../llm/llm-decomposer.js'
import { RuleBasedDecomposer, RuleBasedRiskScanner } from '../llm/rule-decomposer.js'
import type { LlmClient, SemanticRiskScanner, TaskDecomposer } from '../llm/types.js'
import {
  cloneTask,
  newId,
  toTask,
  type GlobalConstraints,
  type ProjectPlan,
  type RiskItem,
  type Task,
  type TaskBase,
} from '../models/entities.js'
import { applyHourBuffers, type BufferAdjustment } from './buffer.js'
import { computeDailyLoad, detectOverloads } from './calendar.js'
import { buildReport, type ReportResult } from './report.js'
import { runHardcodedRiskRules } from './risk.js'

/* -------------------------------------------------------------------------- */
/*                                  异常类型                                    */
/* -------------------------------------------------------------------------- */

/** 存在循环依赖：属于“业务性失败”，调用方应提示用户修正依赖 */
export class PlanCycleError extends Error {
  constructor(readonly cycleNodes: string[]) {
    super(`检测到循环依赖，无法继续规划。环内任务：${cycleNodes.join('、')}`)
    this.name = 'PlanCycleError'
  }
}

/** 计划状态异常：例如任务不存在、字段非法、ID 冲突 */
export class PlanStateError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'PlanStateError'
  }
}

/* -------------------------------------------------------------------------- */
/*                                  配置                                       */
/* -------------------------------------------------------------------------- */

export interface PlannerServiceOptions {
  /** 审计日志器（必填，保证全链路可追溯） */
  audit: AuditLogger
  /** 可选 LLM 客户端：提供则启用 LLM 主路径，否则全程规则实现 */
  llm?: LlmClient
  /** LLM 失败时是否降级到规则实现，默认 true */
  fallbackToRule?: boolean
  /** 单日工时，默认 8 */
  workHoursPerDay?: number
  /** 人员过载阈值（小时），默认取单日工时 */
  overloadThreshold?: number
  /** 缓冲规则表覆盖 */
  bufferRules?: readonly BufferRule[]
  /** 缓冲叠加顺序覆盖 */
  bufferApplyOrder?: readonly ('type' | 'priority')[]
  /** 扩展点（Jira / Git / CI-CD），当前仅保留接口 */
  extensions?: PlannerExtensions
}

/** 任务可更新字段白名单（§四.5） */
export type UpdatableField = 'estimatedHours' | 'predecessorTaskIds' | 'status' | 'priority' | 'assignee'

/* -------------------------------------------------------------------------- */
/*                              PlannerService                                */
/* -------------------------------------------------------------------------- */

/**
 * 迭代规划业务服务。
 *
 * 设计为纯 TS 类（不继承 Cordis Service），便于脱离 Harness 单测与本地 demo；
 * 在插件层通过闭包注入到各工具中，其他插件若需复用可自行包装为 Cordis Service。
 */
export class PlannerService {
  private readonly audit: AuditLogger
  private readonly decomposer: TaskDecomposer
  private readonly riskScanner: SemanticRiskScanner
  private readonly fallbackToRule: boolean
  private readonly workHoursPerDay: number
  private readonly overloadThreshold: number
  private readonly bufferRules: readonly BufferRule[]
  private readonly bufferApplyOrder: readonly ('type' | 'priority')[]
  /** 预留扩展点，当前未启用外部系统 */
  readonly extensions?: PlannerExtensions

  /** 内存中的任务集合（含计算后的 es/ls/slack） */
  private taskList: Task[] = []
  /** 当前原始需求 */
  private requirement = ''
  /** 当前全局约束 */
  private constraints: GlobalConstraints = { iterationDeadline: todayISO(), devMemberCount: 1 }
  /** 最近一次生成的完整计划 */
  private lastPlan?: ProjectPlan
  /** 最近一次缓冲修正明细，供审计与报表说明 */
  private lastBufferAdjustments: BufferAdjustment[] = []

  constructor(options: PlannerServiceOptions) {
    this.audit = options.audit
    this.fallbackToRule = options.fallbackToRule ?? true
    this.workHoursPerDay = options.workHoursPerDay ?? SCHEDULE.WORK_HOURS_PER_DAY
    this.overloadThreshold = options.overloadThreshold ?? this.workHoursPerDay
    this.bufferRules = options.bufferRules ?? BUFFER_RULES
    this.bufferApplyOrder = options.bufferApplyOrder ?? BUFFER_APPLY_ORDER
    this.extensions = options.extensions

    if (options.llm) {
      this.decomposer = new LlmDecomposer(options.llm)
      this.riskScanner = new LlmRiskScanner(options.llm)
    } else {
      this.decomposer = new RuleBasedDecomposer()
      this.riskScanner = new RuleBasedRiskScanner()
    }
  }

  /* ------------------------------ 只读访问 ------------------------------ */

  /** 当前任务集合（拷贝，防止外部直接改动内部状态） */
  getTaskList(): Task[] {
    return this.taskList.map(cloneTask)
  }

  /** 最近一次完整计划 */
  getActivePlan(): ProjectPlan | undefined {
    return this.lastPlan
  }

  /** 最近一次缓冲修正明细 */
  getBufferAdjustments(): BufferAdjustment[] {
    return [...this.lastBufferAdjustments]
  }

  /** 当前拆解策略类型，用于报告与审计 */
  getDecomposerKind(): 'llm' | 'rule' {
    return this.decomposer.kind
  }

  /**
   * 仅执行需求拆解（§四.1 task_decompose），返回不含 es/ls/slack 的原始任务。
   * 注意：本方法不修改内部任务集合，也不触发排期；正式规划请使用 {@link createPlan}。
   */
  async decomposeRequirement(requirement: string, devCount: number, deadline: string): Promise<TaskBase[]> {
    return this.audit.trace(
      'task_decompose',
      { devCount, deadline },
      () => this.decomposeSafely(requirement, { iterationDeadline: deadline, devMemberCount: devCount }),
      (tasks) => ({ count: tasks.length }),
    )
  }

  /* ------------------------------ 完整流程 ------------------------------ */

  /**
   * 完整新建计划（§五 步骤 2~7）：
   * 拆解 → 工时缓冲修正 → DAG 校验 → AOE 排期 → 风险扫描 → 燃尽 → 完整计划。
   */
  async createPlan(requirement: string, constraints: GlobalConstraints): Promise<ProjectPlan> {
    return this.audit.trace(
      'plan_iteration',
      { requirement, constraints },
      async () => {
        this.requirement = requirement
        this.constraints = this.normalizeConstraints(constraints)

        // 步骤 2：需求拆解（LLM 主路径，失败可降级规则）
        const rawTasks = await this.decomposeSafely(requirement, constraints)
        // 工时评估修正（§3.3）
        const normalized = rawTasks.map((t) => toTask(t))
        const buffered = applyHourBuffers(normalized, this.bufferRules, this.bufferApplyOrder)
        this.lastBufferAdjustments = buffered.adjustments
        this.taskList = buffered.tasks

        // 步骤 3~7：DAG 校验 → 排期 → 风险 → 燃尽 → 计划
        return this.computePlan()
      },
      (plan) => ({
        planId: plan.planId,
        taskCount: plan.taskList.length,
        riskCount: plan.riskList.length,
        totalDurationHours: plan.totalDurationHours,
      }),
    )
  }

  /**
   * 全量重规划（§3.5）。执行顺序严格固定：
   * 1. 更新内存任务集合（已由调用方完成）
   * 2. dag_check：存在环直接抛错终止
   * 3. schedule_calc：重算 es/ls/slack、关键路径、总工期
   * 4. risk_scan：全量风险扫描
   * 5. 重新生成燃尽数据
   * 6. 输出完整 ProjectPlan
   */
  async replanAll(constraints?: Partial<GlobalConstraints>): Promise<ProjectPlan> {
    if (constraints) {
      this.constraints = this.normalizeConstraints({ ...this.constraints, ...constraints })
    }
    return this.audit.trace(
      'replan_all',
      { taskCount: this.taskList.length, constraints: this.constraints },
      () => this.computePlan(),
      (plan) => ({ planId: plan.planId, riskCount: plan.riskList.length, totalDurationHours: plan.totalDurationHours }),
    )
  }

  /* ------------------------------ 任务变更 ------------------------------ */

  /** 新增任务（§四.4），随后触发全量重规划 */
  async addTask(draft: Partial<TaskBase> & Pick<TaskBase, 'taskName'>): Promise<{ taskList: Task[]; plan: ProjectPlan }> {
    return this.audit.trace('task_add', { taskName: draft.taskName }, async () => {
      const task = toTask(draft)
      if (draft.taskId && this.taskList.some((t) => t.taskId === draft.taskId)) {
        throw new PlanStateError(`任务 ID 已存在：${draft.taskId}`)
      }
      // 校验前置依赖是否存在
      const known = new Set(this.taskList.map((t) => t.taskId))
      const missing = task.predecessorTaskIds.filter((id) => !known.has(id))
      if (missing.length > 0) {
        throw new PlanStateError(`前置任务不存在：${missing.join('、')}`)
      }
      this.taskList.push(task)
      const plan = await this.computePlan()
      return { taskList: this.getTaskList(), plan }
    })
  }

  /** 修改任务字段（§四.5），随后触发全量重规划 */
  async updateTask(
    taskId: string,
    updateField: UpdatableField,
    newValue: unknown,
  ): Promise<{ task: Task; plan: ProjectPlan }> {
    return this.audit.trace('task_update', { taskId, updateField }, async () => {
      const index = this.taskList.findIndex((t) => t.taskId === taskId)
      if (index < 0) throw new PlanStateError(`任务不存在：${taskId}`)
      const current = this.taskList[index]
      const updated: Task = { ...cloneTask(current), updateTime: Date.now() }

      switch (updateField) {
        case 'estimatedHours': {
          const hours = Number(newValue)
          if (!Number.isFinite(hours) || hours <= 0) throw new PlanStateError(`estimatedHours 必须为正数，收到：${String(newValue)}`)
          updated.estimatedHours = hours
          break
        }
        case 'predecessorTaskIds': {
          if (!Array.isArray(newValue) || newValue.some((v) => typeof v !== 'string')) {
            throw new PlanStateError('predecessorTaskIds 必须为字符串数组')
          }
          const ids = newValue as string[]
          const known = new Set(this.taskList.filter((t) => t.taskId !== taskId).map((t) => t.taskId))
          const missing = ids.filter((id) => !known.has(id))
          if (missing.length > 0) throw new PlanStateError(`前置任务不存在：${missing.join('、')}`)
          if (ids.includes(taskId)) throw new PlanStateError('任务不能依赖自身')
          updated.predecessorTaskIds = ids
          break
        }
        case 'status': {
          if (typeof newValue !== 'string' || !(Object.values(TaskStatus) as string[]).includes(newValue)) {
            throw new PlanStateError(`status 取值非法：${String(newValue)}`)
          }
          updated.status = newValue as TaskStatus
          break
        }
        case 'priority': {
          if (typeof newValue !== 'string' || !['P0', 'P1', 'P2', 'P3'].includes(newValue)) {
            throw new PlanStateError(`priority 取值非法：${String(newValue)}`)
          }
          updated.priority = newValue as Priority
          break
        }
        case 'assignee': {
          updated.assignee = typeof newValue === 'string' ? newValue : undefined
          break
        }
        default: {
          // 类型层面已穷尽，运行时兜底
          throw new PlanStateError(`不支持的更新字段：${String(updateField)}`)
        }
      }

      this.taskList[index] = updated
      const plan = await this.computePlan()
      const result = plan.taskList.find((t) => t.taskId === taskId) ?? updated
      return { task: result, plan }
    })
  }

  /** 删除任务（§四.6）：同时清理其他任务对它的前置引用，随后触发全量重规划 */
  async deleteTask(taskId: string): Promise<{ taskList: Task[]; plan: ProjectPlan }> {
    return this.audit.trace('task_delete', { taskId }, async () => {
      if (!this.taskList.some((t) => t.taskId === taskId)) {
        throw new PlanStateError(`任务不存在：${taskId}`)
      }
      this.taskList = this.taskList
        .filter((t) => t.taskId !== taskId)
        .map((t) => (t.predecessorTaskIds.includes(taskId) ? { ...cloneTask(t), predecessorTaskIds: t.predecessorTaskIds.filter((id) => id !== taskId), updateTime: Date.now() } : t))
      const plan = await this.computePlan()
      return { taskList: this.getTaskList(), plan }
    })
  }

  /* ------------------------------ 独立步骤 ------------------------------ */

  /**
   * 用给定任务集合替换内存状态并全量重规划。
   * 供 replan_all 显式传入 taskList 时使用；不传时沿用当前内存集合。
   */
  async replaceTasksAndReplan(
    drafts: Array<Partial<TaskBase> & Pick<TaskBase, 'taskName'>>,
    constraints?: Partial<GlobalConstraints>,
  ): Promise<ProjectPlan> {
    this.taskList = drafts.map((draft) => toTask(draft))
    return this.replanAll(constraints)
  }

  /** 仅构建 DAG（§四.2 dag_build） */
  dagBuild(): DagGraph {
    return buildDag(this.taskList)
  }

  /** 仅执行 Kahn 校验（§四.3 dag_check） */
  dagCheck(): { graph: DagGraph; result: DagCheckResult } {
    const graph = buildDag(this.taskList)
    return { graph, result: kahnCheck(graph) }
  }

  /** 仅执行 AOE 排期（§四.7 schedule_calc） */
  scheduleCalc(): ScheduleResult {
    const graph = buildDag(this.taskList)
    const dag = kahnCheck(graph)
    if (dag.hasCycle) throw new PlanCycleError(dag.cycleNodes)
    return computeSchedule(this.taskList, dag.topoOrder)
  }

  /** 全量风险扫描（§四.8）：硬编码规则 + 语义扫描。可传入任务集合覆盖默认内存集合 */
  async riskScan(overrideTasks?: readonly Task[]): Promise<RiskItem[]> {
    const tasks = overrideTasks ?? this.taskList
    return this.audit.trace('risk_scan', { taskCount: tasks.length }, async () => {
      const graph = buildDag(tasks)
      const dag = kahnCheck(graph)
      if (dag.hasCycle) throw new PlanCycleError(dag.cycleNodes)
      const schedule = computeSchedule(tasks, dag.topoOrder)
      const loads = computeDailyLoad(tasks, this.constraints.iterationStartDate ?? todayISO(), this.workHoursPerDay)
      const overloads = detectOverloads(loads, this.overloadThreshold)
      const hard = runHardcodedRiskRules({ taskList: tasks, graph, dag, schedule, overloads })
      const semantic = await this.scanSemanticSafely(tasks)
      return [...hard, ...semantic]
    })
  }

  /** 生成报表（§四.10） */
  generateReport(): ReportResult {
    if (!this.lastPlan) throw new PlanStateError('尚无可用计划，请先执行 plan_iteration 或 replan_all')
    return buildReport(this.lastPlan)
  }

  /* ------------------------------ 内部实现 ------------------------------ */

  /** 规范化全局约束，补齐迭代起始日期 */
  private normalizeConstraints(constraints: GlobalConstraints): GlobalConstraints {
    return {
      iterationDeadline: constraints.iterationDeadline || todayISO(),
      iterationStartDate: constraints.iterationStartDate || todayISO(),
      devMemberCount: Number.isFinite(constraints.devMemberCount) && constraints.devMemberCount > 0 ? Math.floor(constraints.devMemberCount) : 1,
    }
  }

  /** 需求拆解，LLM 失败时按配置降级到规则实现 */
  private async decomposeSafely(requirement: string, constraints: GlobalConstraints): Promise<TaskBase[]> {
    const input = { requirement, devCount: constraints.devMemberCount, deadline: constraints.iterationDeadline }
    if (this.decomposer.kind === 'rule') return this.decomposer.decompose(input)
    try {
      return await this.decomposer.decompose(input)
    } catch (error) {
      if (!this.fallbackToRule) throw error
      this.audit.record({
        action: 'task_decompose.degrade',
        ok: true,
        durationMs: 0,
        error: `LLM 拆解失败，已降级为规则实现：${error instanceof Error ? error.message : String(error)}`,
      })
      return new RuleBasedDecomposer().decompose(input)
    }
  }

  /** 语义风险扫描，LLM 失败时按配置降级到规则实现 */
  private async scanSemanticSafely(taskList: readonly Task[]): Promise<RiskItem[]> {
    const input = { requirement: this.requirement, taskList }
    if (this.riskScanner.kind === 'rule') return this.riskScanner.scan(input)
    try {
      return await this.riskScanner.scan(input)
    } catch (error) {
      if (!this.fallbackToRule) throw error
      this.audit.record({
        action: 'risk_scan.degrade',
        ok: true,
        durationMs: 0,
        error: `LLM 语义扫描失败，已降级为规则实现：${error instanceof Error ? error.message : String(error)}`,
      })
      return new RuleBasedRiskScanner().scan(input)
    }
  }

  /**
   * 执行完整流水线并生成 ProjectPlan（§3.5 顺序）。
   * 存在环时抛 {@link PlanCycleError} 立即终止。
   */
  private async computePlan(): Promise<ProjectPlan> {
    // 2. DAG 校验
    const graph = buildDag(this.taskList)
    const dag = kahnCheck(graph)
    if (dag.hasCycle) throw new PlanCycleError(dag.cycleNodes)

    // 悬空依赖仅提示，不参与风险规则（避免偏离规范定义的风险规则集）
    const dangling = findDanglingPredecessors(this.taskList)
    if (dangling.length > 0) {
      this.audit.record({
        action: 'dag_check.dangling',
        ok: true,
        durationMs: 0,
        output: dangling,
        error: '存在悬空前置依赖引用，已在图中忽略',
      })
    }

    // 3. AOE 排期
    const schedule = computeSchedule(this.taskList, dag.topoOrder)
    const computedTasks = this.taskList.map((task) => ({
      ...cloneTask(task),
      es: schedule.esMap[task.taskId] ?? 0,
      ls: schedule.lsMap[task.taskId] ?? 0,
      slack: schedule.slackMap[task.taskId] ?? 0,
    }))
    this.taskList = computedTasks

    // 4. 风险扫描（硬编码 + 语义）
    const startDate = this.constraints.iterationStartDate ?? todayISO()
    const loads = computeDailyLoad(computedTasks, startDate, this.workHoursPerDay)
    const overloads = detectOverloads(loads, this.overloadThreshold)
    const hardRisks = runHardcodedRiskRules({
      taskList: computedTasks,
      graph,
      dag,
      schedule,
      overloads,
    })
    const semanticRisks = await this.scanSemanticSafely(computedTasks)
    const riskList = [...hardRisks, ...semanticRisks]

    // 5. 燃尽数据
    const burndownRawData = buildBurndown({
      taskList: computedTasks,
      iterationStartDate: startDate,
      totalDurationHours: schedule.totalDurationHours,
      workHoursPerDay: this.workHoursPerDay,
    })

    // 6. 完整计划
    const plan: ProjectPlan = {
      planId: newId(),
      rawRequirement: this.requirement,
      iterationDeadline: this.constraints.iterationDeadline,
      iterationStartDate: startDate,
      devMemberCount: this.constraints.devMemberCount,
      taskList: computedTasks,
      riskList,
      criticalPath: schedule.criticalPath,
      burndownRawData,
      totalEstimatedHours: Math.round(sumEstimatedHours(computedTasks) * 100) / 100,
      totalDurationHours: Math.round(schedule.totalDurationHours * 100) / 100,
    }
    this.lastPlan = plan
    return plan
  }
}
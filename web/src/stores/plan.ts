/**
 * 计划 Store：控制台的核心状态与业务动作。
 *
 * 职责：
 *  - 维护计划列表、当前计划、DAG 视图、报表与审计日志；
 *  - 封装所有写操作（新建计划 / 增删改任务 / 重规划），写成功后同步刷新视图；
 *  - 建立 WebSocket 连接，接收其他会话触发的计划变更并自动刷新。
 */

import { ElMessage } from 'element-plus'
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { ApiClientError, api, resolveWsUrl } from '@/api/client'
import type {
  AdviceResult,
  AgentHealth,
  AuditRecord,
  CreatePlanPayload,
  EnumMeta,
  GraphPayload,
  PlanEventMessage,
  PlanSummary,
  ProjectPlan,
  ReplanPayload,
  ReportResult,
  ScenarioCompareResult,
  ScenarioConstraints,
  TaskDraftPayload,
  TaskUpdatePayload,
} from '@/api/types'

/** WebSocket 连接状态 */
export type WsStatus = 'closed' | 'connecting' | 'open'

/** 把异常转换为用户可读文案（保留原错误码供页面差异化处理） */
export function errorText(error: unknown): string {
  if (error instanceof ApiClientError) return error.message
  return error instanceof Error ? error.message : String(error)
}

export const usePlanStore = defineStore('plan', () => {
  /* --------------------------------- 状态 -------------------------------- */

  const summaries = ref<PlanSummary[]>([])
  const enums = ref<EnumMeta | null>(null)
  const plan = ref<ProjectPlan | null>(null)
  const graph = ref<GraphPayload | null>(null)
  const report = ref<ReportResult | null>(null)
  const audit = ref<AuditRecord[]>([])
  const advice = ref<AdviceResult | null>(null)
  const agentHealth = ref<AgentHealth | null>(null)
  const scenario = ref<ScenarioCompareResult | null>(null)

  const listLoading = ref(false)
  const detailLoading = ref(false)
  const mutating = ref(false)
  const adviceLoading = ref(false)
  const scenarioLoading = ref(false)
  const wsStatus = ref<WsStatus>('closed')

  /** 当前计划 ID */
  const currentPlanId = computed(() => plan.value?.planId ?? '')

  /** 按 taskId 建立任务索引，供表格 / 风险面板展示任务名 */
  const taskIndex = computed(() => {
    const map = new Map<string, ProjectPlan['taskList'][number]>()
    for (const task of plan.value?.taskList ?? []) map.set(task.taskId, task)
    return map
  })

  /** 关键路径任务集合 */
  const criticalSet = computed(() => new Set(plan.value?.criticalPath ?? []))

  /** 风险按等级统计 */
  const riskStats = computed(() => {
    const list = plan.value?.riskList ?? []
    return {
      total: list.length,
      high: list.filter((r) => r.level === 'HIGH').length,
      medium: list.filter((r) => r.level === 'MEDIUM').length,
      low: list.filter((r) => r.level === 'LOW').length,
    }
  })

  /** 关键路径总工期（工作日，按单日 8 小时折算） */
  const durationWorkDays = computed(() =>
    plan.value ? Math.max(1, Math.ceil(plan.value.totalDurationHours / 8)) : 0,
  )

  /** 计划完成日期：直接取引擎燃尽数据末点（引擎按工作日推进，前端不重算） */
  const plannedEndDate = computed(() => {
    const points = plan.value?.burndownRawData ?? []
    return points.length > 0 ? points[points.length - 1].time : null
  })

  /** 任务状态统计（仪表盘用） */
  const statusStats = computed(() => {
    const list = plan.value?.taskList ?? []
    const counter = new Map<string, number>()
    for (const task of list) counter.set(task.status, (counter.get(task.status) ?? 0) + 1)
    return [...counter.entries()].map(([status, count]) => ({ status, count }))
  })

  /** 按责任人聚合的工时分布（仅求和展示，不参与任何业务判定） */
  const resourceLoad = computed(() => {
    const critical = criticalSet.value
    const map = new Map<string, { assignee: string; hours: number; taskCount: number; criticalCount: number }>()
    for (const task of plan.value?.taskList ?? []) {
      const key = task.assignee || '待分配'
      const entry = map.get(key) ?? { assignee: key, hours: 0, taskCount: 0, criticalCount: 0 }
      entry.hours += task.estimatedHours
      entry.taskCount += 1
      if (critical.has(task.taskId)) entry.criticalCount += 1
      map.set(key, entry)
    }
    return [...map.values()].sort((a, b) => b.hours - a.hours)
  })

  /**
   * 过载责任人集合（仪表盘高亮用）。
   *
   * 人员过载判定在引擎侧（`src/service/risk.ts` §4.2），此处仅从引擎生成的
   * 风险描述中解析责任人名称用于展示，不重复实现任何判定逻辑。
   */
  const overloadedMembers = computed(() => {
    const names = new Set<string>()
    for (const risk of plan.value?.riskList ?? []) {
      if (!risk.description.includes('单日工时超限')) continue
      for (const match of risk.description.matchAll(/「([^」]+)」/g)) names.add(match[1])
    }
    return names
  })

  /* -------------------------------- 只读加载 ------------------------------- */

  async function loadEnums(): Promise<void> {
    if (enums.value) return
    try {
      enums.value = await api.enums()
    } catch (error) {
      ElMessage.error(`加载枚举元数据失败：${errorText(error)}`)
    }
  }

  async function loadPlans(): Promise<void> {
    listLoading.value = true
    try {
      summaries.value = await api.listPlans()
    } catch (error) {
      ElMessage.error(`加载计划列表失败：${errorText(error)}`)
    } finally {
      listLoading.value = false
    }
  }

  /** 加载计划详情 + DAG + 报表 */
  async function loadPlan(planId: string): Promise<void> {
    detailLoading.value = true
    // 切换到新计划时清空「按需生成」的分析结果，避免展示上一个计划的建议 / 情景
    advice.value = null
    scenario.value = null
    try {
      plan.value = await api.getPlan(planId)
      // DAG 与报表失败不阻断主流程（例如计划为空时报表仍可用）
      const [graphResult, reportResult] = await Promise.allSettled([api.graph(planId), api.report(planId)])
      graph.value = graphResult.status === 'fulfilled' ? graphResult.value : null
      report.value = reportResult.status === 'fulfilled' ? reportResult.value : null
    } catch (error) {
      plan.value = null
      graph.value = null
      report.value = null
      ElMessage.error(`加载计划失败：${errorText(error)}`)
    } finally {
      detailLoading.value = false
    }
  }

  async function loadAudit(planId?: string): Promise<void> {
    try {
      audit.value = await api.audit({ planId, limit: 300 })
    } catch (error) {
      ElMessage.error(`加载审计日志失败：${errorText(error)}`)
    }
  }

  /** 读取 Agent 可用性（是否已配置 LLM 网关），用于风险建议页的模式提示 */
  async function loadAgentHealth(): Promise<void> {
    try {
      agentHealth.value = await api.agentHealth()
    } catch (error) {
      agentHealth.value = null
      ElMessage.error(`获取 Agent 状态失败：${errorText(error)}`)
    }
  }

  /** 生成当前计划的风险缓解建议（LLM 不可用时后端自动降级为规则聚合） */
  async function loadAdvice(): Promise<void> {
    if (!plan.value) return
    adviceLoading.value = true
    try {
      advice.value = await api.advise(plan.value.planId)
    } catch (error) {
      advice.value = null
      ElMessage.error(`生成风险建议失败：${errorText(error)}`)
    } finally {
      adviceLoading.value = false
    }
  }

  /** 执行 What-if 情景对比（后端临时生成计划并即时回收，不改动基线计划） */
  async function runScenarioCompare(constraints: ScenarioConstraints): Promise<boolean> {
    if (!plan.value) return false
    scenarioLoading.value = true
    try {
      scenario.value = await api.scenarioCompare({ planId: plan.value.planId, ...constraints })
      return true
    } catch (error) {
      scenario.value = null
      ElMessage.error(`情景对比失败：${errorText(error)}`)
      return false
    } finally {
      scenarioLoading.value = false
    }
  }

  /** 重新拉取当前计划与 DAG（写操作或外部事件后调用） */
  async function refreshCurrent(): Promise<void> {
    if (!plan.value) return
    const planId = plan.value.planId
    // 计划内容已变化：按需生成的风险建议 / 情景对比结果随之失效
    advice.value = null
    scenario.value = null
    plan.value = await api.getPlan(planId)
    try {
      graph.value = await api.graph(planId)
    } catch {
      // 图中若出现环，graph 接口仍会正常返回 hasCycle=true，不会抛错；此处仅为容错
      graph.value = null
    }
    try {
      report.value = await api.report(planId)
    } catch {
      report.value = null
    }
  }

  /* -------------------------------- 写操作 ------------------------------- */

  async function createPlan(payload: CreatePlanPayload): Promise<ProjectPlan | null> {
    mutating.value = true
    try {
      const created = await api.createPlan(payload)
      ElMessage.success(`计划已生成：共 ${created.taskList.length} 个任务`)
      await loadPlans()
      return created
    } catch (error) {
      ElMessage.error(`创建计划失败：${errorText(error)}`)
      return null
    } finally {
      mutating.value = false
    }
  }

  async function removePlan(planId: string): Promise<void> {
    try {
      await api.deletePlan(planId)
      ElMessage.success('计划已删除')
      if (plan.value?.planId === planId) plan.value = null
      await loadPlans()
    } catch (error) {
      ElMessage.error(`删除计划失败：${errorText(error)}`)
    }
  }

  async function replan(payload: ReplanPayload): Promise<void> {
    if (!plan.value) return
    mutating.value = true
    try {
      plan.value = await api.replan(plan.value.planId, payload)
      ElMessage.success(`重规划完成：总工期 ${plan.value.totalDurationHours}h，风险 ${plan.value.riskList.length} 条`)
      await refreshCurrent()
      await loadPlans()
    } catch (error) {
      ElMessage.error(`重规划失败：${errorText(error)}`)
    } finally {
      mutating.value = false
    }
  }

  async function addTask(draft: TaskDraftPayload): Promise<boolean> {
    if (!plan.value) return false
    mutating.value = true
    try {
      const result = await api.addTask(plan.value.planId, draft)
      plan.value = result.plan
      ElMessage.success(`已新增任务：${result.task.taskName}`)
      await refreshCurrent()
      return true
    } catch (error) {
      ElMessage.error(`新增任务失败：${errorText(error)}`)
      return false
    } finally {
      mutating.value = false
    }
  }

  async function updateTask(taskId: string, payload: TaskUpdatePayload): Promise<boolean> {
    if (!plan.value) return false
    mutating.value = true
    try {
      const result = await api.updateTask(plan.value.planId, taskId, payload)
      plan.value = result.plan
      ElMessage.success('任务已更新，计划已重新规划')
      await refreshCurrent()
      return true
    } catch (error) {
      // 循环依赖：给出针对性提示，引导用户修正依赖
      if (error instanceof ApiClientError && error.code === 'CYCLE_DEPENDENCY') {
        ElMessage.error(`检测到循环依赖，已阻断重规划：${error.message}`)
      } else {
        ElMessage.error(`更新任务失败：${errorText(error)}`)
      }
      return false
    } finally {
      mutating.value = false
    }
  }

  async function deleteTask(taskId: string): Promise<boolean> {
    if (!plan.value) return false
    mutating.value = true
    try {
      const result = await api.deleteTask(plan.value.planId, taskId)
      plan.value = result.plan
      ElMessage.success('任务已删除，引用已同步清理')
      await refreshCurrent()
      return true
    } catch (error) {
      ElMessage.error(`删除任务失败：${errorText(error)}`)
      return false
    } finally {
      mutating.value = false
    }
  }

  async function downloadReport(format: 'json' | 'md'): Promise<void> {
    if (!plan.value) return
    try {
      await api.download(plan.value.planId, format)
    } catch (error) {
      ElMessage.error(`导出失败：${errorText(error)}`)
    }
  }

  /* ------------------------------ WebSocket ------------------------------ */

  let socket: WebSocket | null = null
  let reconnectTimer: number | null = null
  let manualClose = false

  function connect(): void {
    if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return
    manualClose = false
    wsStatus.value = 'connecting'
    try {
      socket = new WebSocket(resolveWsUrl())
    } catch {
      wsStatus.value = 'closed'
      scheduleReconnect()
      return
    }

    socket.onopen = () => {
      wsStatus.value = 'open'
    }
    socket.onmessage = (event: MessageEvent<string>) => {
      void handleMessage(event.data)
    }
    socket.onclose = () => {
      wsStatus.value = 'closed'
      socket = null
      if (!manualClose) scheduleReconnect()
    }
    socket.onerror = () => {
      wsStatus.value = 'closed'
    }
  }

  function scheduleReconnect(): void {
    if (reconnectTimer !== null) return
    reconnectTimer = window.setTimeout(() => {
      reconnectTimer = null
      connect()
    }, 5000)
  }

  function disconnect(): void {
    manualClose = true
    if (reconnectTimer !== null) {
      window.clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    socket?.close()
    socket = null
    wsStatus.value = 'closed'
  }

  async function handleMessage(raw: string): Promise<void> {
    let message: PlanEventMessage
    try {
      message = JSON.parse(raw) as PlanEventMessage
    } catch {
      return
    }
    if (message.type === 'connected' || !message.planId) return

    // 列表摘要始终刷新；若事件命中当前计划，则同步刷新详情
    const isCurrent = plan.value?.planId === message.planId
    if (message.type === 'plan.deleted') {
      if (isCurrent) plan.value = null
      await loadPlans()
      return
    }
    if (isCurrent && message.plan) {
      plan.value = message.plan
      try {
        graph.value = await api.graph(message.planId)
      } catch {
        graph.value = null
      }
    }
    await loadPlans()
  }

  return {
    // 状态
    summaries,
    enums,
    plan,
    graph,
    report,
    audit,
    advice,
    agentHealth,
    scenario,
    listLoading,
    detailLoading,
    mutating,
    adviceLoading,
    scenarioLoading,
    wsStatus,
    // 派生
    currentPlanId,
    taskIndex,
    criticalSet,
    riskStats,
    durationWorkDays,
    plannedEndDate,
    statusStats,
    resourceLoad,
    overloadedMembers,
    // 动作
    loadEnums,
    loadPlans,
    loadPlan,
    loadAudit,
    loadAgentHealth,
    loadAdvice,
    runScenarioCompare,
    refreshCurrent,
    createPlan,
    removePlan,
    replan,
    addTask,
    updateTask,
    deleteTask,
    downloadReport,
    connect,
    disconnect,
  }
})
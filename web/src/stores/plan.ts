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
import { ApiClientError, api, jobStream, resolveWsUrl } from '@/api/client'
import { useNotifyStore } from '@/stores/notify'
import type {
  AdviceResult,
  AgentHealth,
  AuditRecord,
  CreatePlanPayload,
  EnumMeta,
  GraphPayload,
  JobHandle,
  JobKind,
  JobProgressItem,
  JobSnapshot,
  JobStatus,
  PlanEventMessage,
  PlanSummary,
  ProjectPlan,
  ReplanPayload,
  ReportResult,
  ScenarioCompareResult,
  ScenarioConstraints,
  TaskDraftPayload,
  TaskUpdatePayload,
  UpdatableField,
} from '@/api/types'

/** WebSocket 连接状态 */
export type WsStatus = 'closed' | 'connecting' | 'open'

/**
 * 当前正在跑的异步写作业（建计划 / 重规划）。
 *
 * 长耗时写被拆成「提交即返回 + 进度流」后，UI 必须能回答三个问题：**在跑什么**、
 * **跑到哪了**、**能不能停**——这三件事都由这个状态承载。
 */
export interface WriteJobState {
  jobId: string
  kind: JobKind
  /** 人读标签（「新建迭代计划」/「重新规划」），用于进度条标题 */
  label: string
  status: JobStatus
  progress: JobProgressItem[]
  /** 最近一条进度文案（无进度时为「已提交，等待引擎受理」） */
  message: string
  startedAt: number
}

/** 一次异步作业的终态结论（三态互斥，调用方必须穷尽处理） */
type WriteJobOutcome =
  | { kind: 'succeeded'; snapshot: JobSnapshot }
  | { kind: 'failed'; error: { code: string; message: string } }
  | { kind: 'cancelled' }

/** 把异常转换为用户可读文案（保留原错误码供页面差异化处理） */
export function errorText(error: unknown): string {
  if (error instanceof ApiClientError) return error.message
  return error instanceof Error ? error.message : String(error)
}

export const usePlanStore = defineStore('plan', () => {
  /** 通知中心：把平台级事件沉淀为可追溯的「人读摘要」 */
  const notify = useNotifyStore()

  /**
   * 本地写操作时间戳：引擎在任何写操作后都会广播 `plan.updated`，
   * 用它抑制「自己触发的变更」被回灌成外部通知，避免重复提示。
   */
  let lastWriteAt = 0

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
  /** 进行中的异步写作业；null 表示没有长耗时写在跑 */
  const writeJob = ref<WriteJobState | null>(null)

  /** 进度流取消句柄（仅在组件卸载 / 切换作业时中断订阅，不作为「取消作业」手段） */
  let writeAbort: AbortController | null = null

  /** 开始一次本地写操作：置忙并记录时间戳（供 WebSocket 通知去重） */
  function beginWrite(): void {
    mutating.value = true
    lastWriteAt = Date.now()
  }

  /**
   * 跑一次异步写作业：受理回执 → 订阅 SSE 进度 → 归一化为三态结论。
   *
   * **为什么不能只用 HTTP 状态码**：`202 Accepted` 只表示「已受理」，作业此刻**还没落库**。
   * 若把 202 当成功，用户会在进度条还没走完时看到「计划已生成」，随后页面拉取详情失败；
   * 若把「流断开」当失败，又会在引擎其实已经算完时误报失败（孤儿写的老问题换个方向重演）。
   * 因此这里**只认终态**：`succeeded` / `cancelled` / `failed`，其余一律以作业快照接口兜底。
   */
  async function runWriteJob(handle: JobHandle, label: string): Promise<WriteJobOutcome> {
    writeAbort?.abort()
    const controller = new AbortController()
    writeAbort = controller
    writeJob.value = {
      jobId: handle.jobId,
      kind: handle.kind,
      label,
      status: handle.status,
      progress: [],
      message: '已提交，等待引擎受理',
      startedAt: Date.now(),
    }

    let snapshot: JobSnapshot | null = null
    let failure: { code: string; message: string } | null = null

    const applyStatus = (status: JobStatus, message?: string): void => {
      const current = writeJob.value
      if (!current || current.jobId !== handle.jobId) return
      current.status = status
      if (message !== undefined) current.message = message
    }

    try {
      await jobStream(
        handle.jobId,
        {
          onProgress: (item, status) => {
            const current = writeJob.value
            if (!current || current.jobId !== handle.jobId) return
            current.progress = [...current.progress, item]
            applyStatus((status as JobStatus) || 'running', item.message)
          },
          onDone: (done) => {
            snapshot = done
            applyStatus(done.status)
          },
          onError: (error) => {
            failure = error
            applyStatus('failed', error.message)
          },
        },
        controller.signal,
      )
    } catch (error) {
      // ABORTED 是主动中断订阅（组件卸载 / 切换作业），不是作业失败，交给兜底逻辑判定
      if (!(error instanceof ApiClientError && error.code === 'ABORTED')) {
        failure = { code: 'STREAM_ERROR', message: errorText(error) }
      }
    }

    // 流结束但没有终态：以快照接口兜底，避免把「其实还在跑」的作业单方面判失败
    if (!snapshot && !failure && !controller.signal.aborted) {
      try {
        const latest = await api.jobStatus(handle.jobId)
        snapshot = latest
        applyStatus(latest.status)
      } catch (error) {
        failure = { code: 'JOB_STATUS_UNAVAILABLE', message: errorText(error) }
      }
    }

    if (writeAbort === controller) {
      writeAbort = null
      writeJob.value = null
    }

    // 订阅被主动中断（本地上一次作业被新的写操作取代 / 页面卸载）：不再等待结论，
    // 按「已放弃等待」处理——报成「流中断失败」会把主动放弃说成故障，误导用户。
    if (!snapshot && controller.signal.aborted) return { kind: 'cancelled' }

    if (snapshot) {
      if (snapshot.status === 'succeeded') return { kind: 'succeeded', snapshot }
      if (snapshot.status === 'cancelled') return { kind: 'cancelled' }
      return {
        kind: 'failed',
        error: snapshot.error ?? { code: 'JOB_FAILED', message: '作业执行失败' },
      }
    }
    return {
      kind: 'failed',
      error: failure ?? { code: 'STREAM_ERROR', message: '作业进度流中断，请稍后重试' },
    }
  }

  /**
   * 取消进行中的异步写作业。
   *
   * 刻意**不** abort 进度流：终态（`cancelled`）要由服务端经流下发，本地单方面断开会让
   * `runWriteJob` 走兜底分支去查快照，反而多一次请求、且时序更难推理。
   * 引擎侧同步处理无法被单方面中断，取消只保证「不再把结果写回计划」，见后端说明。
   */
  async function cancelWrite(): Promise<void> {
    const current = writeJob.value
    if (!current || current.status === 'succeeded' || current.status === 'failed') return
    try {
      await api.cancelJob(current.jobId)
    } catch (error) {
      ElMessage.error(`取消作业失败：${errorText(error)}`)
    }
  }

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
    beginWrite()
    try {
      // ① 只提交，不等算法算完（引擎侧同步阻塞 80s+，挂连接等必然被中间层超时切断）
      const handle = await api.createPlan(payload)
      // ② 订阅进度流，直到拿到终态
      const outcome = await runWriteJob(handle, '新建迭代计划')
      if (outcome.kind === 'cancelled') {
        ElMessage.info('已取消创建计划')
        return null
      }
      if (outcome.kind === 'failed') {
        ElMessage.error(`创建计划失败：${outcome.error.message}`)
        return null
      }
      const planId = outcome.snapshot.planId
      if (!planId) {
        // 终态报成功却没有 planId = 服务端契约破损：宁可明确报错，绝不假装成功
        ElMessage.error('创建计划失败：服务端未返回计划 ID')
        return null
      }
      // ③ 详情必须另取：作业只回执「算完了」，不把整棵树塞进响应
      const created = await api.getPlan(planId)
      ElMessage.success(`计划已生成：共 ${created.taskList.length} 个任务`)
      notify.push({
        level: created.riskList.length > 0 ? 'warning' : 'success',
        title: '迭代计划已生成',
        detail: `${created.taskList.length} 个任务 · 关键工期 ${created.totalDurationHours}h · 风险 ${created.riskList.length} 条`,
        planId: created.planId,
      })
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
      lastWriteAt = Date.now()
      await api.deletePlan(planId)
      ElMessage.success('计划已删除')
      notify.push({ level: 'info', title: '计划已删除', detail: `planId：${planId}` })
      if (plan.value?.planId === planId) plan.value = null
      await loadPlans()
    } catch (error) {
      ElMessage.error(`删除计划失败：${errorText(error)}`)
    }
  }

  async function replan(payload: ReplanPayload): Promise<void> {
    if (!plan.value) return
    const planId = plan.value.planId
    beginWrite()
    try {
      const handle = await api.replan(planId, payload)
      const outcome = await runWriteJob(handle, '重新规划')
      if (outcome.kind === 'cancelled') {
        ElMessage.info('已取消重规划')
        return
      }
      if (outcome.kind === 'failed') {
        ElMessage.error(`重规划失败：${outcome.error.message}`)
        return
      }
      // 重新拉取权威详情（而不是把作业回执当计划对象）：并发的其他写会让本地副本过时
      await refreshCurrent()
      const current = plan.value
      if (!current) return
      ElMessage.success(`重规划完成：总工期 ${current.totalDurationHours}h，风险 ${current.riskList.length} 条`)
      notify.push({
        level: current.riskList.some((risk) => risk.level === 'HIGH') ? 'warning' : 'success',
        title: '计划已重新规划',
        detail: `关键工期 ${current.totalDurationHours}h · 风险 ${current.riskList.length} 条`,
        planId: current.planId,
      })
      await loadPlans()
    } catch (error) {
      ElMessage.error(`重规划失败：${errorText(error)}`)
    } finally {
      mutating.value = false
    }
  }

  async function addTask(draft: TaskDraftPayload): Promise<boolean> {
    if (!plan.value) return false
    beginWrite()
    try {
      const result = await api.addTask(plan.value.planId, draft)
      plan.value = result.plan
      ElMessage.success(`已新增任务：${result.task.taskName}`)
      notify.push({ level: 'success', title: '已新增任务', detail: result.task.taskName, planId: result.plan.planId })
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
    beginWrite()
    try {
      const result = await api.updateTask(plan.value.planId, taskId, payload)
      plan.value = result.plan
      ElMessage.success('任务已更新，计划已重新规划')
      notify.push({
        level: 'success',
        title: '任务已更新',
        detail: `${result.task.taskName} · ${payload.updateField}`,
        planId: result.plan.planId,
      })
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

  /**
   * 批量编辑任务（复用引擎单任务接口，不在 Python/前端复刻任何重规划逻辑）。
   *
   * 契约：编辑「同一字段、不同任务」为一批，串行提交以规避并发写冲突；
   * 引擎在每次 task_update 后执行其标准全量重规划，前端只在全部提交完成后
   * 统一刷新一次视图，并只产生一条通知，避免批量操作刷屏。
   */
  async function batchUpdateTasks(
    taskIds: string[],
    field: UpdatableField,
    value: unknown,
  ): Promise<{ ok: number; failed: number; error?: string }> {
    if (!plan.value || taskIds.length === 0) return { ok: 0, failed: 0 }
    const planId = plan.value.planId
    beginWrite()
    let ok = 0
    let failed = 0
    let firstError: string | undefined
    try {
      for (const taskId of taskIds) {
        try {
          await api.updateTask(planId, taskId, { updateField: field, newValue: value })
          ok += 1
        } catch (error) {
          failed += 1
          // 循环依赖会阻断后续重规划，继续提交没有意义：立即中止并保留已生效部分
          if (error instanceof ApiClientError && error.code === 'CYCLE_DEPENDENCY') {
            firstError = `检测到循环依赖，已中止批量编辑：${error.message}`
            break
          }
          firstError ??= errorText(error)
        }
      }
      await refreshCurrent()
      await loadPlans()
      if (failed === 0) {
        ElMessage.success(`批量编辑完成：${ok} 个任务已更新`)
        notify.push({
          level: 'success',
          title: '批量编辑完成',
          detail: `${ok} 个任务 · 字段 ${field}`,
          planId,
        })
      } else {
        ElMessage.warning(`批量编辑部分失败：成功 ${ok} 个，失败 ${failed} 个`)
        notify.push({
          level: 'warning',
          title: '批量编辑部分失败',
          detail: `成功 ${ok} 个 · 失败 ${failed} 个 · ${firstError ?? ''}`,
          planId,
        })
      }
      return { ok, failed, error: firstError }
    } finally {
      mutating.value = false
    }
  }

  async function deleteTask(taskId: string): Promise<boolean> {
    if (!plan.value) return false
    beginWrite()
    try {
      const result = await api.deleteTask(plan.value.planId, taskId)
      plan.value = result.plan
      ElMessage.success('任务已删除，引用已同步清理')
      notify.push({ level: 'warning', title: '任务已删除', detail: taskId, planId: result.plan.planId })
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

    // 本地写操作触发的广播（2s 内）不回灌成通知，避免与本地提示重复
    const fromOtherSession = Date.now() - lastWriteAt > 2000

    // 列表摘要始终刷新；若事件命中当前计划，则同步刷新详情
    const isCurrent = plan.value?.planId === message.planId
    if (message.type === 'plan.deleted') {
      if (fromOtherSession) {
        notify.push({
          level: 'warning',
          title: '计划已被其他会话删除',
          detail: message.planId,
        })
      }
      if (isCurrent) plan.value = null
      await loadPlans()
      return
    }
    if (message.type === 'plan.created' && fromOtherSession) {
      notify.push({
        level: 'info',
        title: '其他会话创建了新计划',
        detail: message.plan?.rawRequirement ?? message.planId,
        planId: message.planId,
      })
    }
    if (isCurrent && message.plan) {
      plan.value = message.plan
      if (fromOtherSession) {
        notify.push({
          level: 'info',
          title: '当前计划已被其他会话更新',
          detail: `触发动作：${message.reason ?? 'unknown'}`,
          planId: message.planId,
        })
      }
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
    writeJob,
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
    cancelWrite,
    addTask,
    updateTask,
    batchUpdateTasks,
    deleteTask,
    downloadReport,
    connect,
    disconnect,
  }
})
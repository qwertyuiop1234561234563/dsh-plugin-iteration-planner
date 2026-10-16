/**
 * API 客户端：统一封装 REST 调用、令牌注入、错误归一化与文件导出。
 *
 * 约定：
 *  - 令牌保存在 localStorage，请求拦截器自动注入 `Authorization: Bearer <token>`；
 *  - 后端错误响应体统一为 `{ code, message, details }`，这里归一化为 {@link ApiClientError}，
 *    供页面按 code 做差异化处理（例如 CYCLE_DEPENDENCY 高亮环内任务）。
 */

import axios, { type AxiosError } from 'axios'
import type {
  AdviceResult,
  AgentChatRequest,
  AgentChatResult,
  AgentConfirmResult,
  AgentHealth,
  AgentPendingAction,
  AgentStreamMeta,
  AgentStreamToolResult,
  ApiEnvelope,
  ApiErrorBody,
  AuditRecord,
  BaselineDetail,
  BaselineDiff,
  BaselineSummary,
  BurndownAnalysis,
  BurndownItem,
  CreatePlanPayload,
  CriticalPathResult,
  DagCheckResult,
  DeleteTaskResult,
  EnumMeta,
  GraphPayload,
  HealthResult,
  ImportPayload,
  ImportResult,
  KbDeleteResult,
  KbDocument,
  KbDocumentCreatePayload,
  KbDocumentDetail,
  KbDocumentList,
  KbHitList,
  KbSearchPayload,
  KbStats,
  KbUploadPayload,
  MemoryClearResult,
  MemoryCreatePayload,
  MemoryDeleteResult,
  MemoryItem,
  MemoryList,
  MemorySearchResult,
  MemoryStats,
  PlanSummary,
  PreferenceBlob,
  ProjectPlan,
  QualityResult,
  RegisterItem,
  RegisterListResult,
  RegisterPayload,
  ReplanPayload,
  ReportResult,
  RiskItem,
  ScenarioCompareRequest,
  ScenarioCompareResult,
  TaskDraftPayload,
  TaskHistoryResult,
  TaskMutationResult,
  TaskUpdatePayload,
  WorkloadResult,
} from './types'

/** 令牌本地存储键 */
export const TOKEN_STORAGE_KEY = 'iteration-planner:token'

/** REST 基地址（同域部署时为 /api） */
export const API_BASE = import.meta.env.VITE_API_BASE ?? '/api'

/** 读取访问令牌 */
export function getToken(): string {
  try {
    return localStorage.getItem(TOKEN_STORAGE_KEY) ?? ''
  } catch {
    return ''
  }
}

/** 写入访问令牌（空字符串表示清除） */
export function setToken(token: string): void {
  try {
    if (token) localStorage.setItem(TOKEN_STORAGE_KEY, token)
    else localStorage.removeItem(TOKEN_STORAGE_KEY)
  } catch {
    // 隐私模式下 localStorage 不可用：降级为内存态（本次会话有效）
  }
}

/** 归一化后的 API 异常 */
export class ApiClientError extends Error {
  constructor(
    message: string,
    readonly code: string,
    readonly status: number,
    readonly details?: unknown,
  ) {
    super(message)
    this.name = 'ApiClientError'
  }
}

/** axios 实例 */
export const http = axios.create({
  baseURL: API_BASE,
  timeout: 60_000,
  headers: { 'content-type': 'application/json' },
})

http.interceptors.request.use((config) => {
  const token = getToken()
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

http.interceptors.response.use(
  (response) => response,
  (error: AxiosError<ApiErrorBody>) => {
    const status = error.response?.status ?? 0
    const body = error.response?.data
    const message =
      body?.message ??
      (status === 0 ? '无法连接到服务端，请确认 API 服务已启动' : error.message)
    const code = body?.code ?? (status === 0 ? 'NETWORK_ERROR' : 'HTTP_ERROR')
    return Promise.reject(new ApiClientError(message, code, status, body?.details))
  },
)

/** 解包统一响应包装 `{ data: T }` */
async function unwrap<T>(promise: Promise<{ data: ApiEnvelope<T> }>): Promise<T> {
  const response = await promise
  return response.data.data
}

/** 迭代规划开放 API */
export const api = {
  /** 健康检查 */
  health: () => unwrap<HealthResult>(http.get('/health')),

  /** 枚举元数据（下拉选项与缓冲规则说明） */
  enums: () => unwrap<EnumMeta>(http.get('/meta/enums')),

  /** 计划列表（摘要） */
  listPlans: () => unwrap<PlanSummary[]>(http.get('/plans')),

  /** 计划详情 */
  getPlan: (planId: string) => unwrap<ProjectPlan>(http.get(`/plans/${planId}`)),

  /** 新建计划（完整规划流水线） */
  createPlan: (payload: CreatePlanPayload) => unwrap<ProjectPlan>(http.post('/plans', payload)),

  /** 删除计划 */
  deletePlan: (planId: string) => http.delete(`/plans/${planId}`).then(() => undefined),

  /** 全量重规划 */
  replan: (planId: string, payload: ReplanPayload) => unwrap<ProjectPlan>(http.post(`/plans/${planId}/replan`, payload)),

  /** DAG 视图数据 */
  graph: (planId: string) => unwrap<GraphPayload>(http.get(`/plans/${planId}/graph`)),

  /** 关键路径与总工期（AOE 计算结果） */
  criticalPath: (planId: string) => unwrap<CriticalPathResult>(http.get(`/plans/${planId}/critical-path`)),

  /** 显式执行 Kahn 拓扑校验（存在环时后端返回 409 CYCLE_DEPENDENCY） */
  dagCheck: (planId: string) => unwrap<DagCheckResult>(http.post(`/plans/${planId}/dag-check`)),

  /** 风险清单 */
  risks: (planId: string) => unwrap<RiskItem[]>(http.get(`/plans/${planId}/risks`)),

  /** 燃尽图原始数据 */
  burndown: (planId: string) => unwrap<BurndownItem[]>(http.get(`/plans/${planId}/burndown`)),

  /** 迭代报表（Markdown） */
  report: (planId: string) => unwrap<ReportResult>(http.get(`/plans/${planId}/report`)),

  /** 新增任务 */
  addTask: (planId: string, draft: TaskDraftPayload) =>
    unwrap<TaskMutationResult>(http.post(`/plans/${planId}/tasks`, draft)),

  /** 修改任务字段 */
  updateTask: (planId: string, taskId: string, payload: TaskUpdatePayload) =>
    unwrap<TaskMutationResult>(http.patch(`/plans/${planId}/tasks/${taskId}`, payload)),

  /** 删除任务 */
  deleteTask: (planId: string, taskId: string) =>
    unwrap<DeleteTaskResult>(http.delete(`/plans/${planId}/tasks/${taskId}`)),

  /** 审计日志（可按计划过滤） */
  audit: (params: { planId?: string; limit?: number } = {}) =>
    unwrap<AuditRecord[]>(http.get('/audit', { params })),

  /** Agent 可用性（是否已配置 LLM 网关） */
  agentHealth: () => unwrap<AgentHealth>(http.get('/agent/health')),

  /** 风险缓解建议（LLM 可用时走模型，否则规则聚合降级） */
  advise: (planId: string) => unwrap<AdviceResult>(http.post('/agent/advise', { planId })),

  /* --------------------------- Agent 会话式对话（AI 助手） --------------------------- */

  /** 会话式对话（SSE 流式输出，逐字下发；写操作默认登记为待确认，用户确认后才落库）。 */
  agentChatStream: (payload: AgentChatRequest, handlers: AgentStreamHandlers, signal?: AbortSignal) =>
    agentChatStream(payload, handlers, signal),

  /** 确认执行待确认的写操作；`actionIds` 缺省表示确认全部 */
  agentConfirm: (sessionId: string, actionIds?: string[]) =>
    unwrap<AgentConfirmResult>(
      http.post(
        '/agent/confirm',
        actionIds && actionIds.length ? { sessionId, actionIds } : { sessionId },
        { timeout: 180_000 },
      ),
    ),

  /** 驳回全部待确认写操作（不执行、仅作废；会话上下文保留） */
  agentReject: (sessionId: string) =>
    unwrap<AgentConfirmResult>(http.post('/agent/confirm', { sessionId, reject: true })),

  /** 清空 Agent 会话上下文（含未确认的写操作） */
  agentResetSession: (sessionId: string) =>
    http.delete(`/agent/sessions/${encodeURIComponent(sessionId)}`).then(() => undefined),

  /** What-if 情景对比：以同需求 + 情景约束临时生成计划并与基线对比（后端会即时回收临时计划） */
  scenarioCompare: (payload: ScenarioCompareRequest) =>
    unwrap<ScenarioCompareResult>(http.post('/scenario/compare', payload)),

  /** 导出计划文件（json / md），带令牌下载为本地文件 */
  async download(planId: string, format: 'json' | 'md'): Promise<void> {
    const response = await http.get(`/plans/${planId}/export`, {
      params: { format },
      responseType: 'blob',
    })
    const url = URL.createObjectURL(response.data as Blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `iteration-plan-${planId}.${format}`
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(url)
  },

  /* ------------------------- 治理能力（/workspace/**） ------------------------- */

  /** 数据导入：追加到现有计划或由导入行新建计划 */
  importTasks: (payload: ImportPayload) => unwrap<ImportResult>(http.post('/workspace/import', payload)),

  /** 基线快照列表 */
  listBaselines: (planId: string) =>
    unwrap<{ planId: string; items: BaselineSummary[] }>(http.get(`/workspace/plans/${planId}/baselines`)),

  /** 创建基线快照（快照内容由服务端从引擎读取当前计划） */
  createBaseline: (planId: string, payload: { name: string; note?: string }) =>
    unwrap<BaselineSummary>(http.post(`/workspace/plans/${planId}/baselines`, payload)),

  /** 基线快照详情（含任务快照） */
  getBaseline: (planId: string, baselineId: string) =>
    unwrap<BaselineDetail>(http.get(`/workspace/plans/${planId}/baselines/${baselineId}`)),

  /** 基线 vs 现状偏差对比 */
  baselineDiff: (planId: string, baselineId: string) =>
    unwrap<BaselineDiff>(http.get(`/workspace/plans/${planId}/baselines/${baselineId}/diff`)),

  /** 删除基线快照 */
  deleteBaseline: (planId: string, baselineId: string) =>
    http.delete(`/workspace/plans/${planId}/baselines/${baselineId}`).then(() => undefined),

  /** 人工风险登记册列表（含统计） */
  listRegisters: (planId: string) =>
    unwrap<RegisterListResult>(http.get(`/workspace/plans/${planId}/registers`)),

  /** 登记一条人工风险 */
  createRegister: (planId: string, payload: RegisterPayload) =>
    unwrap<RegisterItem>(http.post(`/workspace/plans/${planId}/registers`, payload)),

  /** 更新人工风险条目（部分字段） */
  updateRegister: (planId: string, issueId: string, payload: Partial<RegisterPayload>) =>
    unwrap<RegisterItem>(http.patch(`/workspace/plans/${planId}/registers/${issueId}`, payload)),

  /** 删除人工风险条目 */
  deleteRegister: (planId: string, issueId: string) =>
    http.delete(`/workspace/plans/${planId}/registers/${issueId}`).then(() => undefined),

  /** 人员负荷与资源平衡 */
  workload: (planId: string) => unwrap<WorkloadResult>(http.get(`/workspace/plans/${planId}/workload`)),

  /** 燃尽增强：审计重建实际剩余曲线 + 偏离度 + 预计完成日 */
  burndownAnalysis: (planId: string) =>
    unwrap<BurndownAnalysis>(http.get(`/workspace/plans/${planId}/burndown-analysis`)),

  /** 任务级变更历史（由审计重建） */
  taskHistory: (planId: string, taskId: string) =>
    unwrap<TaskHistoryResult>(http.get(`/workspace/plans/${planId}/tasks/${taskId}/history`)),

  /** 交付质量看板 */
  quality: (planId: string) => unwrap<QualityResult>(http.get(`/workspace/plans/${planId}/quality`)),

  /** 读取服务端配置备份 */
  getPreferences: (profile?: string) =>
    unwrap<PreferenceBlob>(http.get('/workspace/preferences', { params: profile ? { profile } : {} })),

  /** 写入服务端配置备份 */
  putPreferences: (payload: Record<string, unknown>, profile?: string) =>
    unwrap<{ profile: string; updatedAt: string; bytes: number }>(
      http.put('/workspace/preferences', { payload, ...(profile ? { profile } : {}) }),
    ),

  /** 删除服务端配置备份 */
  deletePreferences: (profile?: string) =>
    http.delete('/workspace/preferences', { params: profile ? { profile } : {} }).then(() => undefined),

  /* --------------------------- 知识库（RAG · /api/kb/**） --------------------------- */

  /** 知识库统计与检索配置 */
  kbStats: () => unwrap<KbStats>(http.get('/kb/stats')),

  /** 文档清单 */
  kbDocuments: () => unwrap<KbDocumentList>(http.get('/kb/documents')),

  /** 以粘贴文本的方式新建文档 */
  kbCreateDocument: (payload: KbDocumentCreatePayload) =>
    unwrap<KbDocument>(http.post('/kb/documents', payload)),

  /** 上传文件建库（multipart；浏览器自动设置 boundary，故不手工指定 content-type） */
  kbUploadDocument: (payload: KbUploadPayload) => {
    const form = new FormData()
    form.append('file', payload.file)
    form.append('title', payload.title ?? '')
    form.append('source', payload.source ?? '')
    form.append('tags', payload.tags ?? '')
    return unwrap<KbDocument>(http.post('/kb/documents/upload', form))
  },

  /** 文档详情（含分块预览） */
  kbGetDocument: (docId: string) =>
    unwrap<KbDocumentDetail>(http.get(`/kb/documents/${encodeURIComponent(docId)}`)),

  /** 删除文档及其全部分块 */
  kbDeleteDocument: (docId: string) =>
    unwrap<KbDeleteResult>(http.delete(`/kb/documents/${encodeURIComponent(docId)}`)),

  /** 混合检索试跑（稠密向量 + BM25 → RRF） */
  kbSearch: (payload: KbSearchPayload) => unwrap<KbHitList>(http.post('/kb/search', payload)),

  /* ------------------------ 长期记忆（Mem0 风格 · /api/memories/**） ------------------------ */

  /** 长期记忆统计 */
  memoryStats: () => unwrap<MemoryStats>(http.get('/memories/stats')),

  /** 记忆清单（可按状态过滤） */
  memories: (params: { status?: string; limit?: number } = {}) =>
    unwrap<MemoryList>(http.get('/memories', { params })),

  /** 手工新增一条长期记忆 */
  createMemory: (payload: MemoryCreatePayload) => unwrap<MemoryItem>(http.post('/memories', payload)),

  /** 按语义召回记忆（试跑） */
  searchMemory: (query: string, topK?: number) =>
    unwrap<MemorySearchResult>(http.post('/memories/search', { query, ...(topK ? { topK } : {}) })),

  /** 启用 / 停用一条记忆 */
  updateMemoryStatus: (memoryId: string, status: string) =>
    unwrap<MemoryItem>(http.patch(`/memories/${encodeURIComponent(memoryId)}`, { status })),

  /** 删除一条记忆 */
  deleteMemory: (memoryId: string) =>
    unwrap<MemoryDeleteResult>(http.delete(`/memories/${encodeURIComponent(memoryId)}`)),

  /** 清空全部记忆（危险操作，前端需二次确认） */
  clearMemories: () => unwrap<MemoryClearResult>(http.delete('/memories')),
}

/* ----------------------- Agent 流式对话（SSE · /agent/chat/stream） ----------------------- */

/** 流式对话事件回调（按需监听，未提供的事件类型自动忽略） */
export interface AgentStreamHandlers {
  /** 首帧：会话 ID + 本轮检索增强上下文（知识库引用 / 长期记忆） */
  onMeta?: (meta: AgentStreamMeta) => void
  /** 助手文本增量（打字机式追加） */
  onDelta?: (text: string) => void
  /** 开始调用某个工具 */
  onToolStart?: (name: string) => void
  /** 工具调用结果（轻量字段，完整明细由 onFinal 补齐） */
  onToolResult?: (record: AgentStreamToolResult) => void
  /** 待用户确认的写操作（确认前不会修改任何数据） */
  onPending?: (actions: AgentPendingAction[]) => void
  /** 终态：完整结果（工具明细 / 计划 / 引用 / 记忆） */
  onFinal?: (result: AgentChatResult) => void
}

/**
 * 解析单个 SSE 帧并回调。
 *
 * 支持：注释行（`: ping` 心跳，忽略）、`event:` / `data:` 字段、CRLF 归一化后的单行 JSON。
 * `error` 事件以 {@link ApiClientError} 抛出，由调用方统一处理（含 LLM 降级判定）。
 */
function dispatchSseFrame(frame: string, handlers: AgentStreamHandlers, status: number): void {
  let name = 'message'
  const dataLines: string[] = []
  for (const line of frame.split('\n')) {
    if (!line || line.startsWith(':')) continue
    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    let value = colon === -1 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'event') name = value
    else if (field === 'data') dataLines.push(value)
  }
  if (dataLines.length === 0) return

  let data: unknown
  try {
    data = JSON.parse(dataLines.join('\n'))
  } catch {
    return
  }

  switch (name) {
    case 'meta':
      handlers.onMeta?.(data as AgentStreamMeta)
      break
    case 'delta':
      handlers.onDelta?.((data as { text?: string }).text ?? '')
      break
    case 'tool_start':
      handlers.onToolStart?.((data as { name?: string }).name ?? '')
      break
    case 'tool_result':
      handlers.onToolResult?.(data as AgentStreamToolResult)
      break
    case 'pending':
      handlers.onPending?.((data as { actions?: AgentPendingAction[] }).actions ?? [])
      break
    case 'final':
      handlers.onFinal?.(data as AgentChatResult)
      break
    case 'error': {
      const body = data as ApiErrorBody
      throw new ApiClientError(
        body?.message ?? '流式响应出错',
        body?.code ?? 'STREAM_ERROR',
        status,
        body?.details,
      )
    }
    default:
      break
  }
}

/**
 * SSE 流式会话对话。
 *
 * 用 `fetch` + `ReadableStream` 读取（axios 无法增量读取响应体），因此需手工注入
 * Bearer 令牌；`AbortSignal` 支持「停止生成」；错误统一归一化为 {@link ApiClientError}
 * （含 `ABORTED` 取消码），与 axios 链路保持一致的错误契约。
 */
export async function agentChatStream(
  payload: AgentChatRequest,
  handlers: AgentStreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const headers: Record<string, string> = {
    'content-type': 'application/json',
    accept: 'text/event-stream',
  }
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`

  let response: Response
  try {
    response = await fetch(`${API_BASE}/agent/chat/stream`, {
      method: 'POST',
      headers,
      body: JSON.stringify(payload),
      signal,
    })
  } catch {
    if (signal?.aborted) throw new ApiClientError('已取消生成', 'ABORTED', 0)
    throw new ApiClientError('无法连接到服务端，请确认 API 服务已启动', 'NETWORK_ERROR', 0)
  }

  if (!response.ok) {
    let body: ApiErrorBody | undefined
    try {
      body = (await response.json()) as ApiErrorBody
    } catch {
      body = undefined
    }
    throw new ApiClientError(
      body?.message ?? `请求失败（HTTP ${response.status}）`,
      body?.code ?? 'HTTP_ERROR',
      response.status,
      body?.details,
    )
  }
  if (!response.body) throw new ApiClientError('服务端未返回流式响应体', 'STREAM_ERROR', response.status)

  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  try {
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      // 归一化 CRLF：JSON 中的换行已被转义，真实 CR 只可能是 SSE 行分隔符
      buffer += decoder.decode(value, { stream: true }).replace(/\r/g, '')
      let boundary = buffer.indexOf('\n\n')
      while (boundary >= 0) {
        const frame = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        dispatchSseFrame(frame, handlers, response.status)
        boundary = buffer.indexOf('\n\n')
      }
    }
  } catch (error) {
    if (error instanceof ApiClientError) throw error
    if (signal?.aborted) throw new ApiClientError('已取消生成', 'ABORTED', 0)
    throw new ApiClientError('流式响应中断，请重试', 'STREAM_ERROR', response.status)
  } finally {
    reader.releaseLock()
  }
}

/** 推导 WebSocket 地址（开发环境由 Vite 代理 /ws） */
export function resolveWsUrl(): string {
  if (import.meta.env.VITE_WS_URL) return import.meta.env.VITE_WS_URL
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}/ws`
}
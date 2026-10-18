/**
 * API 客户端：统一封装 REST 调用、错误归一化与文件导出。
 *
 * 约定：
 *  - **登录态走服务端会话 Cookie**（httpOnly，JS 不可读），不在 localStorage 存放任何凭据；
 *  - 后端错误响应体统一为 `{ code, message, details }`，这里归一化为 {@link ApiClientError}，
 *    供页面按 code 做差异化处理（例如 CYCLE_DEPENDENCY 高亮环内任务）；
 *  - `401` 统一触发 {@link setUnauthorizedHandler} 注册的处理器（路由守卫据此跳登录页）。
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
  AuthAccount,
  AuthAccountCreatePayload,
  AuthAccountList,
  AuthAccountUpdatePayload,
  AuthRoleInfo,
  AuthUser,
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
  JobHandle,
  JobProgressItem,
  JobSnapshot,
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

/** REST 基地址（同域部署时为 /api） */
export const API_BASE = import.meta.env.VITE_API_BASE ?? '/api'

/**
 * 异步写请求首部（RFC 7240 `Prefer`）。
 *
 * 声明「我可以接受受理回执，不等结果」——后端据此走 `202 + 作业资源`，而不是挂住连接
 * 等引擎算完。写成模块级常量是为了让「哪些写是异步的」在这一处可审计。
 */
const PREFER_ASYNC_HEADERS = { Prefer: 'respond-async' } as const

/** 401 处理器：由鉴权 Store 注册，用于跳转登录页并清空本地用户态 */
type UnauthorizedHandler = () => void

let onUnauthorized: UnauthorizedHandler | null = null

/** 注册 401 处理器（重复注册以最后一次为准） */
export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  onUnauthorized = handler
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

  /** 是否为「未登录 / 会话已失效」（服务端 401） */
  get isUnauthorized(): boolean {
    return this.status === 401
  }

  /** 是否为「已登录但权限不足」（服务端 403；重登无效，须调整角色） */
  get isForbidden(): boolean {
    return this.status === 403
  }
}

/** axios 实例 */
export const http = axios.create({
  baseURL: API_BASE,
  // **快速失败**：普通接口应在 30s 内返回。长耗时写（建计划 / 重规划）不再挂在这里等
  // 算法算完，而是走「202 + 作业 + SSE 进度」（见 `createPlanAsync` / `jobStream`），
  // 因此把默认超时收到 30s 只会让「服务端真的卡住」更早暴露，不会误伤正常写。
  timeout: 30_000,
  headers: { 'content-type': 'application/json' },
  // 会话 Cookie 需随请求发送；同源请求默认即带 Cookie，跨域开发场景需显式开启
  withCredentials: true,
})

http.interceptors.response.use(
  (response) => response,
  (error: AxiosError<ApiErrorBody>) => {
    const status = error.response?.status ?? 0
    const body = error.response?.data
    const message =
      body?.message ??
      (status === 0
        ? '无法连接到服务端，请确认 API 服务已启动'
        : // 504 表示「可达但处理超时」：服务端可能仍在计算（引擎无法被单方面中断），
          // 因此提示要看进度而不是直接报「失败」，语义与 RFC 9110 一致。
          status === 504
          ? '服务端仍在处理，请稍后查看进度'
          : error.message)
    const code = body?.code ?? (status === 0 ? 'NETWORK_ERROR' : 'HTTP_ERROR')
    if (status === 401) onUnauthorized?.()
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

  /* ---------------------- 访问控制（/api/auth/**） ---------------------- */

  /** 账号口令登录（成功后服务端下发 httpOnly 会话 Cookie） */
  login: (username: string, password: string) =>
    unwrap<AuthUser>(http.post('/auth/login', { username, password })),

  /** 注销当前会话 */
  logout: () => http.post('/auth/logout').then(() => undefined),

  /** 查询当前登录主体与权限清单（未登录时抛出 401） */
  session: () => unwrap<AuthUser>(http.get('/auth/session')),

  /** 角色与权限矩阵（只读，用于前端展示） */
  authRoles: () => unwrap<{ roles: AuthRoleInfo[] }>(http.get('/auth/roles')),

  /** 修改当前账号口令（需校验原口令；改后其它会话立即失效） */
  changePassword: (currentPassword: string, newPassword: string) =>
    unwrap<{ ok: boolean }>(http.post('/auth/password', { currentPassword, newPassword })),

  /** 账号清单（仅管理员） */
  listUsers: () => unwrap<AuthAccountList>(http.get('/auth/users')),

  /** 新建账号（仅管理员） */
  createUser: (payload: AuthAccountCreatePayload) => unwrap<AuthAccount>(http.post('/auth/users', payload)),

  /** 更新账号（仅管理员） */
  updateUser: (userId: string, payload: AuthAccountUpdatePayload) =>
    unwrap<AuthAccount>(http.patch(`/auth/users/${encodeURIComponent(userId)}`, payload)),

  /** 删除账号（仅管理员） */
  deleteUser: (userId: string) =>
    http.delete(`/auth/users/${encodeURIComponent(userId)}`).then(() => undefined),

  /** 设置访客可见计划白名单（仅管理员） */
  setUserPlans: (userId: string, planIds: string[]) =>
    unwrap<{ userId: string; planIds: string[] }>(
      http.put(`/auth/users/${encodeURIComponent(userId)}/plans`, { planIds }),
    ),

  /** 枚举元数据（下拉选项与缓冲规则说明） */
  enums: () => unwrap<EnumMeta>(http.get('/meta/enums')),

  /** 计划列表（摘要） */
  listPlans: () => unwrap<PlanSummary[]>(http.get('/plans')),

  /** 计划详情 */
  getPlan: (planId: string) => unwrap<ProjectPlan>(http.get(`/plans/${planId}`)),

  /**
   * 新建计划（**异步作业**：`Prefer: respond-async` → `202` + 作业句柄）。
   *
   * 建计划在引擎侧是同步阻塞的算法计算（实测 80s+），挂连接等它算完必然撞上中间层
   * 超时，产生「客户端报失败、引擎已落库」的孤儿写。因此这里只提交、拿回作业 ID，
   * 进度与结果经 {@link jobStream} / {@link api.jobStatus} 获取。
   */
  createPlan: (payload: CreatePlanPayload, signal?: AbortSignal) =>
    unwrap<JobHandle>(http.post('/plans', payload, { headers: PREFER_ASYNC_HEADERS, signal })),

  /** 删除计划 */
  deletePlan: (planId: string) => http.delete(`/plans/${planId}`).then(() => undefined),

  /** 全量重规划（**异步作业**，语义同 {@link api.createPlan}） */
  replan: (planId: string, payload: ReplanPayload, signal?: AbortSignal) =>
    unwrap<JobHandle>(
      http.post(`/plans/${planId}/replan`, payload, { headers: PREFER_ASYNC_HEADERS, signal }),
    ),

  /** 作业快照（状态 / 进度 / 结果）；非属主或不存在的作业一律 404 */
  jobStatus: (jobId: string) => unwrap<JobSnapshot>(http.get(`/jobs/${encodeURIComponent(jobId)}`)),

  /** 取消作业（尽力而为：引擎侧同步处理无法被单方面中断，语义见后端说明） */
  cancelJob: (jobId: string) =>
    unwrap<JobSnapshot>(http.post(`/jobs/${encodeURIComponent(jobId)}/cancel`)),

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
 * 解析单个 SSE 帧：事件名 + JSON 数据。
 *
 * 支持：注释行（`: ping` 心跳，返回 `null` 被忽略）、`event:` / `data:` 字段、多行 `data`
 * 拼接。无数据或非法 JSON 返回 `null`（丢弃而不是抛错——单个坏帧不该中断整条流）。
 */
function parseSseFrame(frame: string): { name: string; data: unknown } | null {
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
  if (dataLines.length === 0) return null
  try {
    return { name, data: JSON.parse(dataLines.join('\n')) }
  } catch {
    return null
  }
}

/**
 * 解析 Agent 流式帧并回调。
 *
 * `error` 事件以 {@link ApiClientError} 抛出，由调用方统一处理（含 LLM 降级判定）。
 */
function dispatchSseFrame(frame: string, handlers: AgentStreamHandlers, status: number): void {
  const parsed = parseSseFrame(frame)
  if (!parsed) return
  const { name, data } = parsed

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
 * 通用 SSE 读取器：`fetch` + `ReadableStream` 增量解析，逐帧回调。
 *
 * 用 `fetch` 而非 axios —— axios（浏览器适配器）无法增量读取响应体。只依赖会话 Cookie
 * 鉴权（同源默认携带，跨域需与服务端 CORS 凭证配置一致），因此不注入任何自定义凭据头。
 * 错误统一归一化为 {@link ApiClientError}（含 `ABORTED` 取消码），与 axios 链路同契约，
 * 调用方可用同一套 `errorText` / 错误码分支处理。
 */
async function streamSse(
  url: string,
  init: { method?: string; body?: string; contentType?: string; signal?: AbortSignal },
  onFrame: (frame: string, status: number) => void,
): Promise<void> {
  const headers: Record<string, string> = { accept: 'text/event-stream' }
  if (init.contentType) headers['content-type'] = init.contentType

  let response: Response
  try {
    response = await fetch(url, {
      method: init.method ?? 'GET',
      headers,
      body: init.body,
      credentials: 'include',
      signal: init.signal,
    })
  } catch {
    if (init.signal?.aborted) throw new ApiClientError('已取消', 'ABORTED', 0)
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
        onFrame(frame, response.status)
        boundary = buffer.indexOf('\n\n')
      }
    }
  } catch (error) {
    if (error instanceof ApiClientError) throw error
    if (init.signal?.aborted) throw new ApiClientError('已取消', 'ABORTED', 0)
    throw new ApiClientError('流式响应中断，请重试', 'STREAM_ERROR', response.status)
  } finally {
    reader.releaseLock()
  }
}

/**
 * SSE 流式会话对话。
 *
 * `AbortSignal` 支持「停止生成」；帧解析与错误归一化复用 {@link streamSse}。
 */
export async function agentChatStream(
  payload: AgentChatRequest,
  handlers: AgentStreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  await streamSse(
    `${API_BASE}/agent/chat/stream`,
    { method: 'POST', body: JSON.stringify(payload), contentType: 'application/json', signal },
    (frame, status) => dispatchSseFrame(frame, handlers, status),
  )
}

/* --------------------------- 异步写作业（/api/jobs/**） --------------------------- */

/** 作业进度流事件回调 */
export interface JobStreamHandlers {
  /** 新进度项（作业内阶段推进） */
  onProgress?: (item: JobProgressItem, status: string) => void
  /** 终态：作业快照（`succeeded` 时 `planId` 非空） */
  onDone?: (snapshot: JobSnapshot) => void
  /** 终态失败：作业快照（含 `error`），或流自身报错时的稳定错误码 */
  onError?: (error: { code: string; message: string }) => void
}

/**
 * 订阅作业进度流（SSE：`progress` → `done` / `error`）。
 *
 * 后端的 `done` / `error` 终态事件携带**作业快照**；而流建立后发生存储故障 / 作业过期时，
 * `error` 事件携带的是标准错误信封（`{code,message}`）。两者都归一化为 `{code,message}`
 * 回调给上层，避免调用方为「作业失败」与「流失败」写两套分支。
 */
export function jobStream(
  jobId: string,
  handlers: JobStreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  return streamSse(`${API_BASE}/jobs/${encodeURIComponent(jobId)}/stream`, { signal }, (frame) => {
    const parsed = parseSseFrame(frame)
    if (!parsed) return
    const { name, data } = parsed
    if (name === 'progress') {
      handlers.onProgress?.(data as JobProgressItem, (data as { status?: string }).status ?? 'running')
      return
    }
    if (name === 'done') {
      handlers.onDone?.(data as JobSnapshot)
      return
    }
    if (name === 'error') {
      const payload = data as Partial<JobSnapshot> & { code?: string; message?: string }
      if (payload.code) {
        handlers.onError?.({ code: payload.code, message: payload.message ?? '作业进度流出错' })
        return
      }
      handlers.onError?.(payload.error ?? { code: 'JOB_FAILED', message: '作业执行失败' })
    }
  })
}

/** 推导 WebSocket 地址（开发环境由 Vite 代理 /ws）
 *
 * 浏览器侧的 `WebSocket` 构造函数**不支持自定义请求头**，因此无法像 REST 那样注入
 * `Authorization`。但浏览器在发起同源 WebSocket 握手时会**自动带上 Cookie**，而服务端
 * `/ws` 已改为按会话 Cookie 鉴权（见 `app/routes/system.py::_resolve_ws_principal`），
 * 所以此处无需再拼接任何凭据，也避免了把令牌暴露在 URL（会被代理与访问日志留存）上。
 *
 * 注意：Cookie 路径须为 `/`，否则 `/ws` 无法携带——服务端写入时已按 `path="/"` 设置。
 */
export function resolveWsUrl(): string {
  return import.meta.env.VITE_WS_URL ?? `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ws`
}
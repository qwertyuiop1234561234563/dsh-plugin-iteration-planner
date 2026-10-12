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
  AgentHealth,
  ApiEnvelope,
  ApiErrorBody,
  AuditRecord,
  BurndownItem,
  CreatePlanPayload,
  CriticalPathResult,
  DagCheckResult,
  DeleteTaskResult,
  EnumMeta,
  GraphPayload,
  HealthResult,
  PlanSummary,
  ProjectPlan,
  ReplanPayload,
  ReportResult,
  RiskItem,
  ScenarioCompareRequest,
  ScenarioCompareResult,
  TaskDraftPayload,
  TaskMutationResult,
  TaskUpdatePayload,
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
}

/** 推导 WebSocket 地址（开发环境由 Vite 代理 /ws） */
export function resolveWsUrl(): string {
  if (import.meta.env.VITE_WS_URL) return import.meta.env.VITE_WS_URL
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.host}/ws`
}
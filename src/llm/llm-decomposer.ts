/**
 * LLM 主路径实现 + Harness LLM 适配器。
 *
 * - {@link LlmDecomposer}：调用 LLM 把大需求拆成原子任务；
 * - {@link LlmRiskScanner}：调用 LLM 做语义风险扫描；
 * - {@link createHarnessLlmClient}：把 Harness 的 `ctx.llm` 适配为 {@link LlmClient}。
 *
 * 异常策略（§7.8）：LLM 输出格式错误抛 {@link LlmFormatError}，
 * 由服务层决定“降级到规则实现”或“向用户报错”，绝不静默崩溃。
 */

import type { Context } from '@deepseek-ai/cordis'
import {
  Priority,
  PRIORITY_VALUES,
  RiskLevel,
  TaskStatus,
  TASK_STATUS_VALUES,
  TaskType,
  TASK_TYPE_VALUES,
} from '../constants/enums.js'
import { newId, newRiskId, type RiskItem, type Task, type TaskBase } from '../models/entities.js'
import { parseJsonLoose } from './json.js'
import {
  LlmFormatError,
  type DecomposeInput,
  type LlmClient,
  type SemanticRiskInput,
  type SemanticRiskScanner,
  type TaskDecomposer,
} from './types.js'

/** 拆解任务 JSON 契约（写入系统提示，约束模型输出） */
const DECOMPOSE_JSON_CONTRACT = `你必须仅输出一个 JSON 数组，不要输出任何解释文字或 Markdown 代码块。
数组每一项字段如下（严禁包含 es/ls/slack）：
{
  "taskName": "string，任务名称",
  "taskDesc": "string，任务详细描述",
  "taskType": "${TASK_TYPE_VALUES.join(' | ')}",
  "estimatedHours": "number，预估工时（小时）",
  "predecessorTaskIds": ["string，前置任务在本次数组中的 taskId，无依赖则为空数组"],
  "status": "${TASK_STATUS_VALUES.join(' | ')}",
  "priority": "${PRIORITY_VALUES.join(' | ')}",
  "assignee": "string，可选"
}`

/** 风险扫描 JSON 契约 */
const RISK_JSON_CONTRACT = `你必须仅输出一个 JSON 数组，不要输出任何解释文字或 Markdown 代码块。
数组每一项字段如下：
{
  "level": "${RiskLevel.HIGH} | ${RiskLevel.MEDIUM} | ${RiskLevel.LOW}",
  "taskIds": ["关联任务ID，可为空数组"],
  "description": "风险描述",
  "suggest": "整改建议"
}`

/** 把任意值强制转为合法枚举，非法则回退默认 */
function coerceEnum<T extends string>(value: unknown, allowed: readonly T[], fallback: T): T {
  return typeof value === 'string' && (allowed as readonly string[]).includes(value) ? (value as T) : fallback
}

/** 规范化 LLM 输出的任务数组 */
export function normalizeDecomposedTasks(raw: unknown): TaskBase[] {
  if (!Array.isArray(raw)) {
    throw new LlmFormatError('task_decompose：LLM 输出不是任务数组')
  }
  const now = Date.now()
  const tasks: TaskBase[] = []
  // 第一遍：生成 ID，保证 predecessorTaskIds 引用可解析
  const idMap = new Map<string, string>()
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue
    const record = item as Record<string, unknown>
    const clientId = typeof record.taskId === 'string' ? record.taskId : undefined
    const generated = newId()
    if (clientId) idMap.set(clientId, generated)
    tasks.push({
      taskId: generated,
      taskName: typeof record.taskName === 'string' && record.taskName.length > 0 ? record.taskName : '未命名任务',
      taskDesc: typeof record.taskDesc === 'string' ? record.taskDesc : '',
      taskType: coerceEnum(record.taskType, TASK_TYPE_VALUES, TaskType.BUSINESS_CODE),
      estimatedHours:
        typeof record.estimatedHours === 'number' && record.estimatedHours > 0 ? record.estimatedHours : 1,
      predecessorTaskIds: [],
      status: coerceEnum(record.status, TASK_STATUS_VALUES, TaskStatus.TODO),
      priority: coerceEnum(record.priority, PRIORITY_VALUES, Priority.P2),
      assignee: typeof record.assignee === 'string' ? record.assignee : undefined,
      createTime: now,
      updateTime: now,
    })
  }
  // 第二遍：映射前置依赖（支持模型用自造 ID 或数组序号引用）
  raw.forEach((item, index) => {
    if (!item || typeof item !== 'object') return
    const record = item as Record<string, unknown>
    const preds = Array.isArray(record.predecessorTaskIds) ? record.predecessorTaskIds : []
    tasks[index].predecessorTaskIds = preds
      .map((p) => {
        if (typeof p !== 'string') return undefined
        return idMap.get(p) ?? (tasks.find((_t, i) => i !== index && String(i) === p)?.taskId)
      })
      .filter((v): v is string => typeof v === 'string')
  })
  return tasks
}

/** 规范化 LLM 输出的风险数组 */
export function normalizeRisks(raw: unknown, taskList: readonly Task[]): RiskItem[] {
  if (!Array.isArray(raw)) {
    throw new LlmFormatError('risk_scan：LLM 输出不是风险数组')
  }
  const knownIds = new Set(taskList.map((t) => t.taskId))
  return raw
    .filter((item): item is Record<string, unknown> => !!item && typeof item === 'object')
    .map((record) => ({
      riskId: newRiskId(),
      level: coerceEnum(record.level, [RiskLevel.HIGH, RiskLevel.MEDIUM, RiskLevel.LOW], RiskLevel.MEDIUM),
      taskIds: Array.isArray(record.taskIds)
        ? record.taskIds.filter((id): id is string => typeof id === 'string' && knownIds.has(id))
        : [],
      description: typeof record.description === 'string' ? record.description : '未命名风险',
      suggest: typeof record.suggest === 'string' ? record.suggest : '请人工复核',
    }))
}

/** LLM 版需求拆解器 */
export class LlmDecomposer implements TaskDecomposer {
  readonly kind = 'llm' as const

  constructor(private readonly client: LlmClient) {}

  async decompose(input: DecomposeInput, signal?: AbortSignal): Promise<TaskBase[]> {
    const system = [
      '你是资深后端研发项目经理，擅长把业务需求拆解为原子研发任务。',
      '任务类型必须从给定枚举中选择；依赖关系用 predecessorTaskIds 表达。',
      DECOMPOSE_JSON_CONTRACT,
    ].join('\n')
    const user = [
      `原始需求：${input.requirement}`,
      `可用开发人数：${input.devCount}`,
      `迭代截止日期：${input.deadline}`,
      '请拆解为覆盖 DB 设计、接口开发、业务代码、联调、测试、部署、文档的原子任务，并给出依赖关系。',
    ].join('\n')
    const text = await this.client.complete({ system, user, signal })
    return normalizeDecomposedTasks(parseJsonLoose<unknown>(text))
  }
}

/** LLM 版语义风险扫描器 */
export class LlmRiskScanner implements SemanticRiskScanner {
  readonly kind = 'llm' as const

  constructor(private readonly client: LlmClient) {}

  async scan(input: SemanticRiskInput, signal?: AbortSignal): Promise<RiskItem[]> {
    const system = [
      '你是资深研发风险识别专家，负责识别需求边界模糊、第三方接口未就绪、测试环境未搭建等非结构化风险。',
      '风险等级依据影响范围判定：影响交付为 HIGH，影响局部为 MEDIUM，提示性为 LOW。',
      RISK_JSON_CONTRACT,
    ].join('\n')
    const taskBrief = input.taskList.map((t) => `- ${t.taskId} | ${t.taskName} | ${t.taskType}`).join('\n')
    const user = [`原始需求：${input.requirement}`, '已拆解任务清单：', taskBrief, '请输出语义风险清单。'].join('\n')
    const text = await this.client.complete({ system, user, signal })
    return normalizeRisks(parseJsonLoose<unknown>(text), input.taskList)
  }
}

/* -------------------------------------------------------------------------- */
/*                          Harness ctx.llm 适配器                             */
/* -------------------------------------------------------------------------- */

/** Harness LLM 运行时在本适配器中的最小依赖面（避免与具体版本强耦合） */
interface HarnessLlmLike {
  stream(options: unknown): AsyncIterable<unknown>
}

export interface HarnessLlmOptions {
  /** 模型提供方，例如 'deepseek' */
  provider: string
  /** 模型名，例如 'deepseek-chat' */
  model: string
}

/** 从流式分片中尽力提取文本增量（不同版本字段可能不同，故做宽松提取） */
function extractChunkText(chunk: unknown): string {
  if (typeof chunk === 'string') return chunk
  if (!chunk || typeof chunk !== 'object') return ''
  const record = chunk as Record<string, unknown>
  for (const key of ['text', 'delta', 'content', 'textDelta']) {
    const value = record[key]
    if (typeof value === 'string') return value
  }
  return ''
}

/**
 * 把 Harness 的 `ctx.llm` 适配为 {@link LlmClient}。
 *
 * 说明：不同 dsh 版本流式分片字段可能不同，本适配器采用宽松提取并在不可用时抛出明确错误，
 * 由服务层降级到规则实现。若目标版本接口有差异，只需调整本函数，不影响其余分层。
 */
export function createHarnessLlmClient(ctx: Context, options: HarnessLlmOptions): LlmClient {
  return {
    name: `harness:${options.provider}/${options.model}`,
    async complete({ system, user, signal }): Promise<string> {
      const llm = (ctx as unknown as { llm?: HarnessLlmLike }).llm
      if (!llm || typeof llm.stream !== 'function') {
        throw new Error('ctx.llm 不可用：请在 Harness 中启用模型提供方，或在插件配置中注入自定义 llm client')
      }
      const stream = llm.stream({
        provider: options.provider,
        model: options.model,
        system,
        messages: [{ role: 'user', content: user }],
        signal,
      })
      let text = ''
      for await (const chunk of stream) {
        text += extractChunkText(chunk)
      }
      return text
    },
  }
}
/**
 * LLM 策略层接口定义。
 *
 * 设计目标（§7.6）：把「拆解」「语义风险扫描」抽象为可替换策略，
 * 从而同时支持：
 *  - LLM 主路径（接入 Harness 的 ctx.llm）；
 *  - 规则降级（无 API Key / 脱离 Harness 时仍可运行 demo 与单测）。
 * 业务代码只依赖接口，不依赖任何具体模型 SDK。
 */

import type { RiskItem, Task, TaskBase } from '../models/entities.js'

/** 底层 LLM 客户端抽象（由插件配置注入实现） */
export interface LlmClient {
  /** 客户端名称，仅用于日志 */
  readonly name: string
  /** 单轮补全，返回模型输出的原始文本 */
  complete(request: { system: string; user: string; signal?: AbortSignal }): Promise<string>
}

/** 拆解入参（§四.1 task_decompose） */
export interface DecomposeInput {
  requirement: string
  devCount: number
  deadline: string
}

/** 需求拆解策略 */
export interface TaskDecomposer {
  readonly kind: 'llm' | 'rule'
  decompose(input: DecomposeInput, signal?: AbortSignal): Promise<TaskBase[]>
}

/** 语义风险扫描入参 */
export interface SemanticRiskInput {
  requirement: string
  taskList: readonly Task[]
}

/** LLM 语义风险扫描策略（§4 尾部） */
export interface SemanticRiskScanner {
  readonly kind: 'llm' | 'rule'
  scan(input: SemanticRiskInput, signal?: AbortSignal): Promise<RiskItem[]>
}

/** LLM 输出格式错误：属于可恢复异常，调用方应降级或提示用户 */
export class LlmFormatError extends Error {
  constructor(message: string, readonly raw?: string) {
    super(message)
    this.name = 'LlmFormatError'
  }
}
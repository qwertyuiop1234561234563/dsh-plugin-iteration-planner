/**
 * 全链路审计日志（§7.5）：每次工具调用、DAG 变更、重规划动作都记录入参、出参、耗时。
 *
 * 本模块不依赖框架：通过注入 `write` 回调输出，插件中接 Harness 的 `ctx.logger`，
 * demo / 单测中接 console。审计内容是结构化 JSON，便于后续接入可观测平台。
 */

import { AUDIT } from '../constants/enums.js'

/** 审计事件载荷 */
export interface AuditEntry {
  /** 动作名，例如 'task_decompose' / 'replan_all' / 'dag_check' */
  action: string
  /** 是否成功 */
  ok: boolean
  /** 耗时（毫秒） */
  durationMs: number
  /** 入参（已截断） */
  input?: unknown
  /** 出参摘要（已截断） */
  output?: unknown
  /** 失败原因 */
  error?: string
}

/** 截断超长字符串，避免审计日志被大对象撑爆 */
function truncate(value: unknown): unknown {
  try {
    const json = JSON.stringify(value)
    if (json === undefined) return value
    if (json.length <= AUDIT.MAX_STRING_LEN) return JSON.parse(json)
    return `${json.slice(0, AUDIT.MAX_STRING_LEN)}…(truncated)`
  } catch {
    return String(value)
  }
}

/** 审计日志器 */
export class AuditLogger {
  constructor(private readonly write: (line: string) => void) {}

  /** 记录一条已完成的审计事件 */
  record(entry: AuditEntry): void {
    const payload = {
      ts: new Date().toISOString(),
      action: entry.action,
      ok: entry.ok,
      durationMs: entry.durationMs,
      input: entry.input === undefined ? undefined : truncate(entry.input),
      output: entry.output === undefined ? undefined : truncate(entry.output),
      error: entry.error,
    }
    this.write(`${AUDIT.PREFIX} ${JSON.stringify(payload)}`)
  }

  /**
   * 包裹一次业务动作，自动测量耗时并记录入参 / 出参 / 异常。
   * 异常会被原样抛出，保证调用方仍能感知失败。
   */
  async trace<T>(action: string, input: unknown, fn: () => Promise<T>, summarize?: (value: T) => unknown): Promise<T> {
    const start = Date.now()
    try {
      const result = await fn()
      this.record({
        action,
        ok: true,
        durationMs: Date.now() - start,
        input,
        output: summarize ? summarize(result) : result,
      })
      return result
    } catch (error) {
      this.record({
        action,
        ok: false,
        durationMs: Date.now() - start,
        input,
        error: error instanceof Error ? `${error.name}: ${error.message}` : String(error),
      })
      throw error
    }
  }
}

/** 默认控制台审计输出 */
export function createConsoleSink(): (line: string) => void {
  return (line: string) => {
    // 审计日志走 stderr，避免污染工具的标准输出
    process.stderr.write(`${line}\n`)
  }
}
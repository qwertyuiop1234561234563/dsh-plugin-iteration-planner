/**
 * 审计桥接：把服务层 {@link AuditLogger} 的文本输出落库为结构化审计记录。
 *
 * 服务层审计器只依赖 `(line: string) => void` 的 sink（框架无侵入），
 * 因此这里把 `[iteration-planner/audit] {json}` 解析回对象并写入 {@link AuditRepository}，
 * 同时转发到运行日志，形成「可查询 + 可观测」的双通道。
 */

import { AUDIT } from '../constants/enums.js'
import type { AuditEntry } from '../logger/audit.js'
import { newAuditRecordId } from '../repository/postgres.js'
import type { AuditRecord, AuditRepository } from '../repository/types.js'

/** 运行日志最小面（与 Fastify logger / Harness logger 兼容） */
export interface RuntimeLogger {
  info(message: string): void
  error(message: string): void
}

/** 审计桥接配置 */
export interface AuditBridgeOptions {
  repository: AuditRepository
  /** 归属计划 ID（插件级事件可为空） */
  planId?: string
  /** 运行日志（可选） */
  logger?: RuntimeLogger
  /** 额外监听（例如推送给前端） */
  onRecord?: (record: AuditRecord) => void
}

/** 解析服务层审计文本行 */
function parseAuditLine(line: string): AuditEntry | undefined {
  const prefix = `${AUDIT.PREFIX} `
  const json = line.startsWith(prefix) ? line.slice(prefix.length) : line
  try {
    const parsed = JSON.parse(json) as AuditEntry & { ts?: string }
    if (!parsed || typeof parsed !== 'object' || typeof parsed.action !== 'string') return undefined
    return parsed
  } catch {
    return undefined
  }
}

/**
 * 创建可持久化的审计 sink。
 * 写入失败（例如数据库抖动）不抛出，避免审计故障阻断主业务流程，但会输出明确错误日志。
 */
export function createPersistingAuditSink(options: AuditBridgeOptions): (line: string) => void {
  return (line: string) => {
    const entry = parseAuditLine(line)
    options.logger?.info(line)
    if (!entry) return

    const record: AuditRecord = {
      recordId: newAuditRecordId(),
      planId: options.planId,
      ts: new Date().toISOString(),
      action: entry.action,
      ok: entry.ok,
      durationMs: entry.durationMs,
      input: entry.input,
      output: entry.output,
      error: entry.error,
    }

    void options.repository.append(record).catch((error: unknown) => {
      options.logger?.error(
        `[iteration-planner/api] 审计日志落库失败：${error instanceof Error ? error.message : String(error)}`,
      )
    })
    options.onRecord?.(record)
  }
}

/** 基于标准错误流的运行日志（独立部署 / 单测形态） */
export function createStdLogger(): RuntimeLogger {
  const write = (message: string): void => {
    process.stderr.write(`${message}\n`)
  }
  return { info: write, error: write }
}
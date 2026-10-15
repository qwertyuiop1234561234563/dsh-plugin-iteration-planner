/**
 * 配置备份与恢复（本地 localStorage ↔ 文件 / 服务端）。
 *
 * 设计要点：
 *  - **不备份访问令牌**：令牌属于凭据，导出为明文文件会扩大泄露面；恢复后需重新填写；
 *  - 备份内容为「偏好快照」的键值对（主题、表格密度、通知、保存视图、迭代模板），
 *    结构上带 `version` 与 `exportedAt`，便于后续字段演进时做兼容判断；
 *  - 恢复为**覆盖式**：先做结构与大小校验，再整批写入，避免部分写入造成半坏状态。
 */

import { TOKEN_STORAGE_KEY } from '@/api/client'

/** 备份文件结构版本 */
export const PREFERENCE_BACKUP_VERSION = 1

/** 备份文本的最大字节数（与 Python `app.constants.MAX_PREFERENCE_BYTES` 对齐，避免本地恢复写入超限数据） */
export const MAX_PREFERENCE_BYTES = 256 * 1024

/** 参与备份的本地存储键（与各模块的 STORAGE_KEY 一致） */
export const BACKUP_STORAGE_KEYS = [
  'iteration-planner:theme',
  'iteration-planner:theme:compact',
  'iteration-planner:notifications',
  'iteration-planner:task-views',
  'iteration-planner:templates',
] as const

/** 备份文件结构 */
export interface PreferenceBackup {
  version: number
  exportedAt: string
  /** 键值快照（键为 localStorage 键名） */
  entries: Record<string, string>
}

/** 安全读取（隐私模式下不可用时返回 null） */
function safeGet(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

/** 安全写入（失败静默返回 false） */
function safeSet(key: string, value: string): boolean {
  try {
    localStorage.setItem(key, value)
    return true
  } catch {
    return false
  }
}

/** 采集当前偏好快照 */
export function collectPreferenceBackup(): PreferenceBackup {
  const entries: Record<string, string> = {}
  for (const key of BACKUP_STORAGE_KEYS) {
    const value = safeGet(key)
    if (value !== null) entries[key] = value
  }
  return { version: PREFERENCE_BACKUP_VERSION, exportedAt: new Date().toISOString(), entries }
}

/** 备份快照 → 供下载 / 上传的 JSON 文本 */
export function backupToJson(backup: PreferenceBackup): string {
  return JSON.stringify(backup, null, 2)
}

/** 校验备份结构（返回错误说明，null 表示合法） */
export function validateBackup(input: unknown): string | null {
  if (typeof input !== 'object' || input === null) return '备份内容不是 JSON 对象'
  const candidate = input as Partial<PreferenceBackup>
  if (typeof candidate.version !== 'number') return '缺少 version 字段'
  if (candidate.version > PREFERENCE_BACKUP_VERSION) return `备份版本（v${candidate.version}）高于当前客户端支持的 v${PREFERENCE_BACKUP_VERSION}`
  const entries = candidate.entries
  if (typeof entries !== 'object' || entries === null || Array.isArray(entries)) return '缺少 entries 键值快照'
  for (const [key, value] of Object.entries(entries)) {
    if (!(BACKUP_STORAGE_KEYS as readonly string[]).includes(key)) return `包含未知配置项：${key}`
    if (typeof value !== 'string') return `配置项 ${key} 的取值必须是字符串`
    // 逐项做 JSON 往返校验，防止把坏数据写回本地
    if (key === 'iteration-planner:theme' && value !== 'light' && value !== 'dark') return '主题取值非法（应为 light / dark）'
    if (key === 'iteration-planner:theme:compact' && value !== '0' && value !== '1') return '表格密度取值非法（应为 0 / 1）'
    if (key !== 'iteration-planner:theme' && key !== 'iteration-planner:theme:compact') {
      try {
        JSON.parse(value)
      } catch {
        return `配置项 ${key} 不是合法 JSON`
      }
    }
  }
  return null
}

/** 解析备份文本（返回 `{ backup }` 或 `{ error }`） */
export function parseBackupText(text: string): { backup?: PreferenceBackup; error?: string } {
  const size = new TextEncoder().encode(text).length
  if (size > MAX_PREFERENCE_BYTES) {
    return { error: `备份体积 ${(size / 1024).toFixed(1)} KB 超过上限 ${MAX_PREFERENCE_BYTES / 1024} KB` }
  }
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch (error) {
    return { error: `JSON 解析失败：${(error as Error).message}` }
  }
  const invalid = validateBackup(parsed)
  if (invalid) return { error: invalid }
  const backup = parsed as PreferenceBackup
  return { backup: { version: backup.version, exportedAt: String(backup.exportedAt ?? ''), entries: backup.entries } }
}

/** 应用备份（覆盖式写入；返回实际写入的键数量） */
export function applyPreferenceBackup(backup: PreferenceBackup, options: { includeToken?: boolean } = {}): number {
  let written = 0
  for (const [key, value] of Object.entries(backup.entries)) {
    if (key === TOKEN_STORAGE_KEY && !options.includeToken) continue
    if (safeSet(key, value)) written += 1
  }
  return written
}

/** 摘要信息（用于 UI 展示备份内容概览） */
export function describeBackup(backup: PreferenceBackup): Array<{ key: string; label: string; size: number }> {
  const LABELS: Record<string, string> = {
    'iteration-planner:theme': '主题模式',
    'iteration-planner:theme:compact': '表格密度',
    'iteration-planner:notifications': '通知中心',
    'iteration-planner:task-views': '保存的筛选视图',
    'iteration-planner:templates': '自定义迭代模板',
  }
  return Object.entries(backup.entries).map(([key, value]) => ({
    key,
    label: LABELS[key] ?? key,
    size: value.length,
  }))
}

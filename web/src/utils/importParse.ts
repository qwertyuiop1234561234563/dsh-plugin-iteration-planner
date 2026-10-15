/**
 * 数据导入解析与前端预校验。
 *
 * 目标：让「导出的 CSV / JSON 能原样导回」——列定义与 {@link taskExportColumns} 对齐，
 * 同时接受英文字段名与常见别名，降低人工准备数据的成本。
 *
 * 边界（与后端 `app/routes/workspace.py` 的分工）：
 *  - 前端负责**文本 → 结构化行**的解析与即时预校验，让错误在提交前就能看到；
 *  - 服务端做同一套规则的权威校验（枚举取值来自引擎 `/meta/enums`）；
 *  - 图算法（环路检测）**不在前端实现**：依赖环由引擎在新增任务后的标准重规划中判定。
 */

import {
  ISSUE_PROBABILITY_VALUES,
  ISSUE_SEVERITY_VALUES,
  ISSUE_STATUS_VALUES,
  PRIORITY_VALUES,
  TASK_STATUS_VALUES,
  TASK_TYPE_VALUES,
  type EnumMeta,
  type ImportRowPayload,
  type Task,
} from '@/api/types'
import { STATUS_LABEL, TASK_TYPE_LABEL } from '@/utils/format'

/** 单行解析结果（含未识别的列与逐行错误） */
export interface ParsedRow extends ImportRowPayload {
  /** 该行在源文本中的序号（从 1 开始，含表头时为数据行序号） */
  row: number
  /** 未能识别的列名（仅用于提示） */
  unknownColumns?: string[]
}

/** 单条行级错误 */
export interface RowIssue {
  row: number
  field: string
  message: string
}

/** 解析结果 */
export interface ParseOutcome {
  rows: ParsedRow[]
  errors: RowIssue[]
  /** 解析器给出的整体提示（例如未识别列、空文件） */
  warnings: string[]
}

/** 中文表头 / 英文键名 → 标准字段名 */
const HEADER_ALIAS: Record<string, string> = {
  任务名称: 'taskName',
  任务名: 'taskName',
  名称: 'taskName',
  taskname: 'taskName',
  name: 'taskName',
  任务描述: 'taskDesc',
  描述: 'taskDesc',
  taskdesc: 'taskDesc',
  desc: 'taskDesc',
  description: 'taskDesc',
  类型: 'taskType',
  任务类型: 'taskType',
  tasktype: 'taskType',
  type: 'taskType',
  状态: 'status',
  任务状态: 'status',
  status: 'status',
  优先级: 'priority',
  priority: 'priority',
  责任人: 'assignee',
  负责人: 'assignee',
  执行人: 'assignee',
  assignee: 'assignee',
  预估工时: 'estimatedHours',
  '预估工时(h)': 'estimatedHours',
  '预估工时（h）': 'estimatedHours',
  工时: 'estimatedHours',
  estimatedhours: 'estimatedHours',
  hours: 'estimatedHours',
  前置任务: 'predecessorRefs',
  前置: 'predecessorRefs',
  前置任务id: 'predecessorRefs',
  前置任务ID: 'predecessorRefs',
  predecessortaskids: 'predecessorRefs',
  predecessorrefs: 'predecessorRefs',
  dependencies: 'predecessorRefs',
}

/** 前置引用分隔符（导出用 ` / `，人工填写常用 `、；,|`） */
const REF_SPLIT = /[、,;|]|\s+\/\s+|\s+/

/** 导入模板（首行即导出列的子集，保证往返一致） */
export const IMPORT_TEMPLATE_CSV = [
  '任务名称,任务描述,类型,状态,优先级,责任人,预估工时(h),前置任务',
  '数据库设计,梳理实体与索引,数据库设计,待办,P0,张三,16,',
  '接口开发,REST 接口实现,接口开发,待办,P1,张三,24,数据库设计',
  '联调自测,前后端联调,联调自测,待办,P1,李四,12,接口开发',
].join('\r\n')

/** 解析分隔文本（CSV / TSV）：支持引号包裹、字段内换行、CRLF 与 BOM */
export function parseDelimited(text: string, delimiter = ','): string[][] {
  const clean = text.replace(/^\uFEFF/, '')
  const rows: string[][] = []
  let row: string[] = []
  let cell = ''
  let quoted = false

  for (let index = 0; index < clean.length; index += 1) {
    const char = clean[index]
    if (quoted) {
      if (char === '"') {
        if (clean[index + 1] === '"') {
          cell += '"'
          index += 1
        } else {
          quoted = false
        }
      } else {
        cell += char
      }
      continue
    }
    if (char === '"') {
      quoted = true
    } else if (char === delimiter) {
      row.push(cell)
      cell = ''
    } else if (char === '\n') {
      row.push(cell)
      rows.push(row)
      row = []
      cell = ''
    } else if (char !== '\r') {
      cell += char
    }
  }
  row.push(cell)
  rows.push(row)
  return rows.filter((item) => item.some((value) => value.trim() !== ''))
}

/** 任务名称长度上限（与 Python `app.constants.IMPORT_NAME_MAX_LEN` 及 ImportRow 的 max_length 对齐） */
export const IMPORT_NAME_MAX_LEN = 200

/** 猜测分隔符：取首行中出现最多的制表符 / 逗号 / 分号（部分 Excel 区域设置导出为分号分隔） */
function guessDelimiter(sample: string): string {
  const candidates: Array<[string, number]> = [
    ['\t', (sample.match(/\t/g) ?? []).length],
    [',', (sample.match(/,/g) ?? []).length],
    [';', (sample.match(/;/g) ?? []).length],
  ]
  const best = candidates.reduce((max, item) => (item[1] > max[1] ? item : max))
  return best[1] > 0 ? best[0] : ','
}

/** 文案 / 枚举双向下拉值：把「接口开发」这类中文文案归一为枚举值 `API_DEV` */
function normalizeEnum(value: string | undefined, map: Record<string, string>): string | undefined {
  if (!value) return undefined
  const text = value.trim()
  const upper = text.toUpperCase().replace(/[\s-]/g, '_')
  if (Object.prototype.hasOwnProperty.call(map, upper)) return upper
  const hit = Object.entries(map).find(([, label]) => label === text)
  return hit ? hit[0] : upper
}

/** 把一行对象（来源 CSV 或 JSON）规范化为 {@link ParsedRow} */
function toRow(source: Record<string, unknown>, rowNumber: number, warnings: string[]): ParsedRow {
  const unknownColumns: string[] = []
  const normalized: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(source)) {
    const field = HEADER_ALIAS[key.trim().toLowerCase()] ?? HEADER_ALIAS[key.trim()]
    if (!field) {
      if (key.trim() !== '') unknownColumns.push(key)
      continue
    }
    normalized[field] = value
  }

  const asText = (value: unknown): string | undefined => {
    if (value === null || value === undefined) return undefined
    if (Array.isArray(value)) return value.map((item) => String(item).trim()).filter(Boolean).join(' / ')
    const text = String(value).trim()
    return text === '' ? undefined : text
  }

  const hoursRaw = normalized.estimatedHours
  const hours =
    hoursRaw === undefined || hoursRaw === null || hoursRaw === ''
      ? undefined
      : typeof hoursRaw === 'number'
        ? hoursRaw
        : Number(String(hoursRaw).replace(/[^\d.\-]/g, ''))

  const refsRaw = normalized.predecessorRefs
  const refs = Array.isArray(refsRaw)
    ? refsRaw.map((item) => String(item).trim()).filter(Boolean)
    : (refsRaw === undefined ? [] : String(refsRaw).split(REF_SPLIT).map((item) => item.trim()).filter(Boolean))

  const row: ParsedRow = {
    row: rowNumber,
    taskName: asText(normalized.taskName) ?? '',
    predecessorRefs: refs,
  }
  const desc = asText(normalized.taskDesc)
  if (desc) row.taskDesc = desc
  const assignee = asText(normalized.assignee)
  if (assignee) row.assignee = assignee
  if (hours !== undefined) row.estimatedHours = Number.isFinite(hours) ? hours : Number.NaN
  const taskType = asText(normalized.taskType)
  if (taskType) row.taskType = taskType
  const status = asText(normalized.status)
  if (status) row.status = status
  const priority = asText(normalized.priority)
  if (priority) row.priority = priority
  if (unknownColumns.length > 0) {
    row.unknownColumns = unknownColumns
    warnings.push(`第 ${rowNumber} 行存在未识别列：${unknownColumns.join('、')}`)
  }
  return row
}

/** 从计划导出 JSON（含 `taskList`）还原为导入行 */
function rowsFromPlan(plan: Record<string, unknown>): Record<string, unknown>[] {
  const list = plan.taskList
  if (!Array.isArray(list)) return []
  return list.map((task) => {
    const item = task as Partial<Task> & Record<string, unknown>
    return {
      taskName: item.taskName,
      taskDesc: item.taskDesc,
      taskType: item.taskType,
      status: item.status,
      priority: item.priority,
      assignee: item.assignee,
      estimatedHours: item.estimatedHours,
      // 导出 JSON 中前置为 taskId；若与本计划同源可原样引用（服务端校验既有任务 ID）
      predecessorRefs: item.predecessorTaskIds,
    }
  })
}

/**
 * 解析导入文本。
 *
 * @param text 源文本（CSV / TSV / JSON / 计划导出 JSON）
 * @param enums 引擎枚举元数据（用于把中文文案归一为枚举值）
 * @param existingTaskIds 目标计划中已存在的任务 ID（前置引用可直接指向它们）
 */
export function parseImportText(
  text: string,
  enums: EnumMeta,
  existingTaskIds: Iterable<string> = [],
): ParseOutcome {
  const warnings: string[] = []
  const trimmed = text.trim()
  if (trimmed === '') return { rows: [], errors: [], warnings: ['内容为空'] }

  const typeMap: Record<string, string> = { ...TASK_TYPE_LABEL }
  const statusMap: Record<string, string> = { ...STATUS_LABEL }
  const priorityMap: Record<string, string> = {}

  let rawRows: Record<string, unknown>[] = []

  if (trimmed.startsWith('[') || trimmed.startsWith('{')) {
    let parsed: unknown
    try {
      parsed = JSON.parse(trimmed)
    } catch (error) {
      return { rows: [], errors: [], warnings: [`JSON 解析失败：${(error as Error).message}`] }
    }
    const container = Array.isArray(parsed)
      ? { tasks: parsed }
      : ((parsed as Record<string, unknown>).data as Record<string, unknown> | undefined) ?? (parsed as Record<string, unknown>)
    if (Array.isArray(container)) {
      rawRows = container as Record<string, unknown>[]
    } else if (Array.isArray((container as Record<string, unknown>).tasks)) {
      rawRows = (container as { tasks: Record<string, unknown>[] }).tasks
    } else if (Array.isArray((container as Record<string, unknown>).taskList)) {
      rawRows = rowsFromPlan(container as Record<string, unknown>)
      warnings.push('检测到计划导出结构，已按任务清单展开为导入行')
    } else {
      return { rows: [], errors: [], warnings: ['JSON 结构无法识别：应为任务数组、{tasks:[...]} 或计划导出对象'] }
    }
  } else {
    const table = parseDelimited(trimmed, guessDelimiter(trimmed.split('\n')[0]))
    if (table.length === 0) return { rows: [], errors: [], warnings: ['内容为空'] }
    const header = table[0].map((cell) => cell.trim())
    const body = table.slice(1)
    rawRows = body.map((cells) => {
      const item: Record<string, unknown> = {}
      header.forEach((key, index) => {
        if (key === '') return
        item[key] = cells[index] ?? ''
      })
      return item
    })
    if (body.length === 0) warnings.push('仅检测到表头，没有数据行')
  }

  const rows: ParsedRow[] = []
  rawRows.forEach((source, index) => {
    const row = toRow(source, index + 1, warnings)
    row.taskType = normalizeEnum(row.taskType, typeMap)
    row.status = normalizeEnum(row.status, statusMap)
    row.priority = normalizeEnum(row.priority, priorityMap)
    rows.push(row)
  })

  const errors = validateRows(rows, enums, existingTaskIds)
  return { rows, errors, warnings }
}

/**
 * 前端预校验（与后端同一套规则）：名称必填且批内唯一、枚举合法、工时为正、
 * 前置引用须为已存在任务 ID 或本批次中更早的行。
 */
export function validateRows(
  rows: ParsedRow[],
  enums: EnumMeta,
  existingTaskIds: Iterable<string> = [],
): RowIssue[] {
  const errors: RowIssue[] = []
  const known = new Set(existingTaskIds)
  const nameIndex = new Map<string, number>()

  rows.forEach((row, position) => {
    const name = (row.taskName ?? '').trim()
    if (!name) {
      errors.push({ row: row.row, field: 'taskName', message: '任务名称不能为空' })
      return
    }
    if (name.length > IMPORT_NAME_MAX_LEN) {
      errors.push({ row: row.row, field: 'taskName', message: `任务名称不得超过 ${IMPORT_NAME_MAX_LEN} 字符` })
      return
    }
    if (nameIndex.has(name)) {
      errors.push({ row: row.row, field: 'taskName', message: `任务名称与第 ${nameIndex.get(name)! + 1} 行重复：${name}` })
      return
    }
    nameIndex.set(name, position)
  })

  rows.forEach((row, position) => {
    if (row.taskType && !enums.taskType.includes(row.taskType as never)) {
      errors.push({ row: row.row, field: 'taskType', message: `任务类型非法：${row.taskType}；可选值：${enums.taskType.join(' / ')}` })
    }
    if (row.status && !enums.taskStatus.includes(row.status as never)) {
      errors.push({ row: row.row, field: 'status', message: `任务状态非法：${row.status}；可选值：${enums.taskStatus.join(' / ')}` })
    }
    if (row.priority && !enums.priority.includes(row.priority as never)) {
      errors.push({ row: row.row, field: 'priority', message: `优先级非法：${row.priority}；可选值：${enums.priority.join(' / ')}` })
    }
    if (row.estimatedHours !== undefined && !(row.estimatedHours > 0)) {
      errors.push({ row: row.row, field: 'estimatedHours', message: '预估工时必须为正数' })
    }
    for (const ref of row.predecessorRefs ?? []) {
      const target = ref.trim()
      if (target === '') {
        errors.push({ row: row.row, field: 'predecessorRefs', message: '前置引用不能为空' })
        continue
      }
      if (known.has(target)) continue
      const earlier = nameIndex.get(target)
      if (earlier === undefined) {
        errors.push({ row: row.row, field: 'predecessorRefs', message: `前置引用无法解析：${target}（须为已存在任务的 ID 或本批次中更早的任务名称）` })
      } else if (earlier >= position) {
        errors.push({ row: row.row, field: 'predecessorRefs', message: `前置引用必须指向本批次中更早的行：${target}` })
      }
    }
  })
  return errors
}

/** 只保留提交给服务端的字段（去掉解析过程产生的行号与提示） */
export function toPayloadRows(rows: ParsedRow[]): ImportRowPayload[] {
  return rows.map((row) => {
    const payload: ImportRowPayload = { taskName: row.taskName.trim() }
    if (row.taskDesc) payload.taskDesc = row.taskDesc
    if (row.taskType) payload.taskType = row.taskType
    if (row.estimatedHours !== undefined && Number.isFinite(row.estimatedHours)) payload.estimatedHours = row.estimatedHours
    if (row.predecessorRefs && row.predecessorRefs.length > 0) payload.predecessorRefs = row.predecessorRefs
    if (row.status) payload.status = row.status
    if (row.priority) payload.priority = row.priority
    if (row.assignee) payload.assignee = row.assignee
    return payload
  })
}

/** 把后端返回的 details 归一为行级错误列表（兼容 FastAPI 字段级校验结构与逐行落库失败结构） */
export function normalizeServerErrors(details: unknown, fallbackMessage = ''): RowIssue[] {
  if (!details) return []
  if (Array.isArray(details)) {
    return details.map((item) => {
      const entry = item as { loc?: unknown[]; msg?: string; field?: unknown; message?: unknown }
      // 本服务的 RequestValidationError 处理器把 Pydantic 错误改写为 {field:"rows.0.taskName", message}；
      // FastAPI 原生结构为 {loc:[...,'rows',0,'taskName'], msg}。两种都按 dotted/数组路径解析出行号。
      if (Array.isArray(entry.loc)) {
        const rowIndex = entry.loc.findIndex((part) => part === 'rows')
        const rowValue = rowIndex >= 0 ? Number(entry.loc[rowIndex + 1]) : Number.NaN
        const field = String(entry.loc[entry.loc.length - 1] ?? '')
        return {
          row: Number.isFinite(rowValue) ? rowValue + 1 : 0,
          field: HEADER_ALIAS[field] ?? field,
          message: entry.msg ?? '请求校验失败',
        }
      }
      const parts = String(entry.field ?? '').split('.')
      const rowIndex = parts.findIndex((part) => part === 'rows')
      const rowValue = rowIndex >= 0 ? Number(parts[rowIndex + 1]) : Number.NaN
      const leaf = parts[parts.length - 1] ?? ''
      return {
        row: Number.isFinite(rowValue) ? rowValue + 1 : 0,
        field: leaf && leaf !== '(root)' ? (HEADER_ALIAS[leaf] ?? leaf) : '',
        message: String(entry.message ?? '请求校验失败'),
      }
    })
  }
  const container = details as { errors?: unknown; row?: unknown; taskName?: unknown }
  if (Array.isArray(container.errors)) {
    return (container.errors as Array<{ row?: number; field?: string; message?: string }>).map((item) => ({
      row: Number(item.row ?? 0),
      field: item.field ?? '',
      message: item.message ?? '校验失败',
    }))
  }
  // 逐行落库失败：服务端返回 {row, taskName, rolledBackTasks, ...}（行号为批次内 1-based 序号，
  // 与前端预览行号口径一致），此处按单行错误关联到预览表，避免只有全局提示而无法定位失败行。
  if (typeof container.row === 'number' && container.row > 0) {
    const name = typeof container.taskName === 'string' ? container.taskName : ''
    return [
      {
        row: container.row,
        field: '',
        message: fallbackMessage || (name ? `任务「${name}」导入失败` : '该行导入失败'),
      },
    ]
  }
  return []
}

/** 校验通过后，枚举字段以「文案」显示时的列宽友好文本 */
export function rowPreviewText(row: ParsedRow): string {
  const parts: string[] = []
  if (row.taskType) parts.push(TASK_TYPE_LABEL[row.taskType as keyof typeof TASK_TYPE_LABEL] ?? row.taskType)
  if (row.status) parts.push(STATUS_LABEL[row.status as keyof typeof STATUS_LABEL] ?? row.status)
  if (row.priority) parts.push(row.priority)
  if (row.estimatedHours !== undefined) parts.push(`${row.estimatedHours}h`)
  if (row.assignee) parts.push(row.assignee)
  return parts.join(' · ')
}

/** 供 UI 显示的枚举取值清单（避免在模板中硬编码字符串） */
export const IMPORT_ENUM_HINTS = {
  taskType: TASK_TYPE_VALUES,
  taskStatus: TASK_STATUS_VALUES,
  priority: PRIORITY_VALUES,
  issueSeverity: ISSUE_SEVERITY_VALUES,
  issueProbability: ISSUE_PROBABILITY_VALUES,
  issueStatus: ISSUE_STATUS_VALUES,
} as const

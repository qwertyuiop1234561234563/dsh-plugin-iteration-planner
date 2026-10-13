/**
 * 数据导出工具（CSV / 制表符文本）。
 *
 * 设计取舍：
 *  - 不引入 xlsx 等重依赖，统一导出 **UTF-8 BOM 的 CSV**：Excel / WPS / Numbers
 *    双击即可正确识别中文与列分隔，等价于「Excel 导出」；
 *  - 所有数据均来自引擎已有的 REST 返回值（计划 / 任务 / 审计），前端只做**格式化**，
 *    不参与任何业务口径计算；
 *  - 对以 `= + - @` 开头的单元格做前缀转义，规避 Excel CSV 公式注入（CWE-1236）。
 */

/** 导出列定义 */
export interface ExportColumn<T> {
  /** 列标题（写入首行） */
  label: string
  /** 取值函数 */
  value: (row: T) => string | number | boolean | null | undefined
}

/** CSV 单元格转义：引号包裹 + 双引号加倍 + 公式前缀转义 */
function escapeCell(input: string | number | boolean | null | undefined): string {
  if (input === null || input === undefined) return ''
  let text = String(input)
  if (/^[=+\-@\t\r]/.test(text)) text = `'${text}`
  if (/[",\n\r]/.test(text)) return `"${text.replace(/"/g, '""')}"`
  return text
}

/** 生成 CSV 文本（含 UTF-8 BOM，Excel 兼容） */
export function buildCsv<T>(columns: Array<ExportColumn<T>>, rows: T[]): string {
  const header = columns.map((column) => escapeCell(column.label)).join(',')
  const body = rows.map((row) => columns.map((column) => escapeCell(column.value(row))).join(','))
  return `\uFEFF${[header, ...body].join('\r\n')}\r\n`
}

/** 触发生成本地文件下载 */
export function downloadText(filename: string, text: string, mime = 'text/csv;charset=utf-8'): void {
  const blob = new Blob([text], { type: mime })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  // 释放对象 URL，避免内存泄漏
  URL.revokeObjectURL(url)
}

/** 导出 CSV 文件 */
export function exportCsv<T>(filename: string, columns: Array<ExportColumn<T>>, rows: T[]): void {
  downloadText(filename, buildCsv(columns, rows))
}

/** 时间戳（毫秒）→ `YYYYMMDD-HHmm`，用于文件名 */
export function fileStamp(date = new Date()): string {
  const pad = (n: number): string => String(n).padStart(2, '0')
  return `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}-${pad(date.getHours())}${pad(date.getMinutes())}`
}

/**
 * 工作日纯函数（**展示层镜像**，非业务算法）。
 *
 * ⚠️ 单一事实来源是引擎 `src/algorithm/workday.ts`：排期、燃尽、缓冲等所有
 * 业务口径均由 TypeScript 引擎计算并经 REST 返回。本文件只服务于「时间轴刻度」
 * 这类纯展示需求（例如甘特图的日期列），不参与任何业务判定，也不改写引擎结果。
 *
 * 为保证与引擎逐日对齐，这里采用与引擎一致的 UTC 日历日语义（周一~周五为工作日）。
 */

/** 单开发人单日最大工时（小时），与引擎 `SCHEDULE.WORK_HOURS_PER_DAY` 一致（仅用于展示换算） */
export const WORK_HOURS_PER_DAY = 8

/** 解析 `YYYY-MM-DD`（或完整 ISO）为 UTC 零点日期 */
export function parseISODate(iso: string): Date {
  const datePart = iso.slice(0, 10)
  const [y, m, d] = datePart.split('-').map((s) => Number.parseInt(s, 10))
  if (!y || !m || !d) throw new Error(`无效的 ISO 日期: ${iso}`)
  return new Date(Date.UTC(y, m - 1, d))
}

/** 将 Date 格式化为 `YYYY-MM-DD` */
export function formatISODate(date: Date): string {
  const y = date.getUTCFullYear()
  const m = String(date.getUTCMonth() + 1).padStart(2, '0')
  const d = String(date.getUTCDate()).padStart(2, '0')
  return `${y}-${m}-${d}`
}

/** 是否为周末（周六 / 周日） */
export function isWeekend(date: Date): boolean {
  const day = date.getUTCDay()
  return day === 0 || day === 6
}

/** 自然日加减 */
export function addDays(date: Date, days: number): Date {
  const next = new Date(date.getTime())
  next.setUTCDate(next.getUTCDate() + days)
  return next
}

/** 从起始日向后推进 n 个工作日，返回 `YYYY-MM-DD` */
export function addWorkdays(startISO: string, n: number): string {
  let cursor = parseISODate(startISO)
  while (isWeekend(cursor)) cursor = addDays(cursor, 1)
  let remaining = n
  while (remaining > 0) {
    cursor = addDays(cursor, 1)
    if (!isWeekend(cursor)) remaining -= 1
  }
  return formatISODate(cursor)
}

/** 返回 [startISO, endISO] 区间内的全部工作日（含端点） */
export function enumerateWorkdays(startISO: string, endISO: string): string[] {
  const result: string[] = []
  let cursor = parseISODate(startISO)
  const end = parseISODate(endISO)
  while (cursor.getTime() <= end.getTime()) {
    if (!isWeekend(cursor)) result.push(formatISODate(cursor))
    cursor = addDays(cursor, 1)
  }
  return result
}

/** 从起始日连续取 n 个工作日（用于生成甘特图日期列） */
export function takeWorkdays(startISO: string, n: number): string[] {
  const result: string[] = []
  for (let index = 0; index < n; index += 1) result.push(addWorkdays(startISO, index))
  return result
}

/** 工时 → 工作日数（向上取整，至少 1） */
export function hoursToWorkDayCount(hours: number): number {
  return Math.max(1, Math.ceil(hours / WORK_HOURS_PER_DAY))
}

/** 判断目标 ISO 日期是否晚于参考 ISO 日期（ISO 日期可直接字典序比较） */
export function isAfterISO(target: string, reference: string): boolean {
  return target > reference
}
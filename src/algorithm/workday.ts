/**
 * 工作日 / 日期纯函数工具（算法层）。
 *
 * 说明：为保证跨时区、跨机器结果可复现，统一以 UTC 处理“日历日”，
 * 只做自然日加减与周末判定（周一~周五为工作日），不引入任何外部依赖。
 */

/** 解析 `YYYY-MM-DD` 或完整 ISO 字符串为 UTC 零点日期 */
export function parseISODate(iso: string): Date {
  const datePart = iso.slice(0, 10)
  const [y, m, d] = datePart.split('-').map((s) => Number.parseInt(s, 10))
  if (!y || !m || !d) {
    throw new Error(`无效的 ISO 日期: ${iso}`)
  }
  return new Date(Date.UTC(y, m - 1, d))
}

/** 将 Date 格式化为 `YYYY-MM-DD` */
export function formatISODate(date: Date): string {
  const y = date.getUTCFullYear()
  const m = String(date.getUTCMonth() + 1).padStart(2, '0')
  const d = String(date.getUTCDate()).padStart(2, '0')
  return `${y}-${m}-${d}`
}

/** 当前日期（UTC）的 `YYYY-MM-DD` */
export function todayISO(): string {
  return formatISODate(new Date())
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

/** 从起始日开始向后推进 n 个工作日，返回 `YYYY-MM-DD`；n=0 时返回不早于起始日的第一个工作日 */
export function addWorkdays(startISO: string, n: number): string {
  let cursor = parseISODate(startISO)
  // 先归位到工作日
  while (isWeekend(cursor)) {
    cursor = addDays(cursor, 1)
  }
  let remaining = n
  while (remaining > 0) {
    cursor = addDays(cursor, 1)
    if (!isWeekend(cursor)) {
      remaining -= 1
    }
  }
  return formatISODate(cursor)
}

/** 返回 [startISO, endISO] 区间内的全部工作日（含端点），用于燃尽与排期展示 */
export function enumerateWorkdays(startISO: string, endISO: string): string[] {
  const result: string[] = []
  let cursor = parseISODate(startISO)
  const end = parseISODate(endISO)
  while (cursor.getTime() <= end.getTime()) {
    if (!isWeekend(cursor)) {
      result.push(formatISODate(cursor))
    }
    cursor = addDays(cursor, 1)
  }
  return result
}
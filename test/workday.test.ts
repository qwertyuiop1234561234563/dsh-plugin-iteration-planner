/**
 * 工作日 / 日历工具单元测试（算法层纯函数）。
 *
 * 基准：2026-10-09 为周五，10-10/10-11 为周末，10-12 为周一。
 */

import { describe, expect, it } from 'vitest'
import { addWorkdays, enumerateWorkdays, formatISODate, isWeekend, parseISODate, todayISO } from '../src/algorithm/workday.js'

describe('工作日数学', () => {
  it('parseISODate / formatISODate 往返一致', () => {
    expect(formatISODate(parseISODate('2026-10-09'))).toBe('2026-10-09')
    expect(formatISODate(parseISODate('2026-10-09T12:34:56Z'))).toBe('2026-10-09')
  })

  it('todayISO 返回合法的 YYYY-MM-DD', () => {
    expect(todayISO()).toMatch(/^\d{4}-\d{2}-\d{2}$/)
  })

  it('isWeekend 正确识别周末', () => {
    expect(isWeekend(parseISODate('2026-10-10'))).toBe(true) // 周六
    expect(isWeekend(parseISODate('2026-10-11'))).toBe(true) // 周日
    expect(isWeekend(parseISODate('2026-10-09'))).toBe(false) // 周五
    expect(isWeekend(parseISODate('2026-10-12'))).toBe(false) // 周一
  })

  it('addWorkdays 跳过周末', () => {
    expect(addWorkdays('2026-10-09', 0)).toBe('2026-10-09') // 周五 + 0 天
    expect(addWorkdays('2026-10-09', 1)).toBe('2026-10-12') // 跨过周末到周一
    expect(addWorkdays('2026-10-09', 2)).toBe('2026-10-13') // 周二
  })

  it('addWorkdays 起点为周末时先归位到工作日', () => {
    expect(addWorkdays('2026-10-10', 0)).toBe('2026-10-12') // 周六归位到周一
  })

  it('enumerateWorkdays 返回区间内全部工作日', () => {
    expect(enumerateWorkdays('2026-10-09', '2026-10-13')).toEqual(['2026-10-09', '2026-10-12', '2026-10-13'])
  })
})
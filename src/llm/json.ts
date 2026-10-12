/**
 * LLM 输出 JSON 容错解析（§7.8：LLM 输出格式错误要有明确处理，不能静默崩溃）。
 *
 * 常见脏输出：被 ```json 代码块包裹、前后有解释性文字、含尾随逗号、使用中文引号。
 * 本模块依次尝试：直接解析 → 提取首个 JSON 片段 → 轻量修复后重试。
 */

import { LlmFormatError } from './types.js'

/** 去掉 markdown 代码围栏 */
function stripCodeFence(text: string): string {
  const fence = /```(?:json|JSON)?\s*([\s\S]*?)```/
  const matched = fence.exec(text)
  return matched ? matched[1] : text
}

/** 截取首个 `[`/`{` 到最后一个 `]`/`}` 之间的内容 */
function extractJsonSlice(text: string): string | undefined {
  const firstObj = text.indexOf('{')
  const firstArr = text.indexOf('[')
  const candidates = [firstObj, firstArr].filter((i) => i >= 0)
  if (candidates.length === 0) return undefined
  const start = Math.min(...candidates)
  const endObj = text.lastIndexOf('}')
  const endArr = text.lastIndexOf(']')
  const end = Math.max(endObj, endArr)
  if (end <= start) return undefined
  return text.slice(start, end + 1)
}

/** 轻量修复：去除尾随逗号、规范化中文引号 */
function repair(text: string): string {
  return text
    .replace(/[\u201c\u201d]/g, '"')
    .replace(/[\u2018\u2019]/g, "'")
    .replace(/,\s*([}\]])/g, '$1')
}

/**
 * 宽松解析 LLM 返回的 JSON。
 * @throws {LlmFormatError} 全部策略失败时抛出，携带原始输出便于排障。
 */
export function parseJsonLoose<T>(raw: string): T {
  const attempts: string[] = []
  const stripped = stripCodeFence(raw).trim()
  attempts.push(stripped)
  const slice = extractJsonSlice(stripped)
  if (slice) attempts.push(slice, repair(slice))
  attempts.push(repair(stripped))

  for (const candidate of attempts) {
    try {
      return JSON.parse(candidate) as T
    } catch {
      // 继续下一次尝试
    }
  }
  throw new LlmFormatError('LLM 输出不是合法 JSON，已尝试代码块剥离与轻量修复', raw)
}
/**
 * OpenAI 兼容 LLM 客户端（独立部署形态）。
 *
 * 说明：
 *  - 挂载到 Harness 内部时，使用 Harness 提供的 `ctx.llm`（见 `createHarnessLlmClient`）；
 *  - 独立启动 / Docker 部署时，通过本客户端对接任意 OpenAI Chat Completions 兼容网关
 *    （DeepSeek、vLLM、Azure OpenAI、OneAPI 等），从而启用 LLM 主路径。
 */

import type { LlmClient } from '../llm/types.js'

export interface OpenAiCompatibleOptions {
  /** 基础地址，例如 https://api.deepseek.com/v1 */
  baseUrl: string
  /** API Key */
  apiKey: string
  /** 模型名，例如 deepseek-chat */
  model: string
  /** 采样温度，默认 0.2 */
  temperature?: number
  /** 单次请求超时（毫秒），默认 60000 */
  timeoutMs?: number
}

/** 从 OpenAI 兼容响应中提取首条消息文本 */
function extractContent(payload: unknown): string {
  if (!payload || typeof payload !== 'object') return ''
  const choices = (payload as { choices?: unknown }).choices
  if (!Array.isArray(choices) || choices.length === 0) return ''
  const message = (choices[0] as { message?: { content?: unknown } }).message
  const content = message?.content
  return typeof content === 'string' ? content : ''
}

/** 构造 OpenAI 兼容 LLM 客户端 */
export function createOpenAiCompatibleClient(options: OpenAiCompatibleOptions): LlmClient {
  const url = `${options.baseUrl.replace(/\/+$/, '')}/chat/completions`
  return {
    name: `openai-compatible:${options.model}`,
    async complete({ system, user, signal }): Promise<string> {
      // 合并外部取消信号与超时信号，任一触发即中止请求
      const controller = new AbortController()
      const timeout = setTimeout(() => controller.abort(), options.timeoutMs ?? 60_000)
      const onAbort = (): void => controller.abort()
      signal?.addEventListener('abort', onAbort, { once: true })
      try {
        const response = await fetch(url, {
          method: 'POST',
          headers: {
            'content-type': 'application/json',
            authorization: `Bearer ${options.apiKey}`,
          },
          body: JSON.stringify({
            model: options.model,
            temperature: options.temperature ?? 0.2,
            messages: [
              { role: 'system', content: system },
              { role: 'user', content: user },
            ],
          }),
          signal: controller.signal,
        })
        if (!response.ok) {
          const detail = await response.text().catch(() => '')
          throw new Error(`LLM 网关返回 ${response.status}：${detail.slice(0, 300)}`)
        }
        return extractContent(await response.json())
      } catch (error) {
        if (error instanceof Error && error.name === 'AbortError') {
          throw new Error('LLM 请求超时或被取消')
        }
        throw error
      } finally {
        clearTimeout(timeout)
        signal?.removeEventListener('abort', onAbort)
      }
    },
  }
}
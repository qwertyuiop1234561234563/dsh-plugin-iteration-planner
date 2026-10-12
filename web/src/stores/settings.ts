/**
 * 设置 Store：访问令牌与偏好项（持久化到 localStorage）。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { getToken, setToken } from '@/api/client'

const THEME_STORAGE_KEY = 'iteration-planner:theme'

export const useSettingsStore = defineStore('settings', () => {
  /** API 访问令牌（与后端 API_TOKEN 对应） */
  const token = ref<string>(getToken())
  /** 是否开启高对比风险高亮 */
  const highlightRisk = ref(true)
  /** 紧凑表格模式 */
  const compactTable = ref(
    (() => {
      try {
        return localStorage.getItem(`${THEME_STORAGE_KEY}:compact`) === '1'
      } catch {
        return false
      }
    })(),
  )

  /** 是否已配置令牌 */
  const hasToken = computed(() => token.value.length > 0)

  function saveToken(value: string): void {
    token.value = value.trim()
    setToken(token.value)
  }

  function toggleCompact(value: boolean): void {
    compactTable.value = value
    try {
      localStorage.setItem(`${THEME_STORAGE_KEY}:compact`, value ? '1' : '0')
    } catch {
      // 忽略存储异常
    }
  }

  return { token, hasToken, highlightRisk, compactTable, saveToken, toggleCompact }
})
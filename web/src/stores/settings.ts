/**
 * 设置 Store：主题模式与偏好项（持久化到 localStorage）。
 *
 * 注：登录态由 `stores/auth.ts` 管理（服务端会话 Cookie，不在本地存凭据）。
 *
 * 主题只影响**展示层**：通过 `data-theme` 属性驱动 CSS 变量切换（见 `styles/main.css`
 * 的「夜间蓝晒」令牌块），并同步 ECharts 主题（`utils/palette.ts`）。不涉及任何业务口径。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { applyChartTheme } from '@/utils/palette'

/** 主题模式 */
export type ThemeMode = 'light' | 'dark'

const THEME_STORAGE_KEY = 'iteration-planner:theme'

/** 安全读取 localStorage（隐私模式下不可用时降级为默认值） */
function readStorage(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

/** 写入 localStorage，失败静默（仅本次会话生效） */
function writeStorage(key: string, value: string): void {
  try {
    localStorage.setItem(key, value)
  } catch {
    // 忽略存储异常
  }
}

/** 读取初始主题：优先本地存储，其次跟随系统偏好 */
function initialTheme(): ThemeMode {
  const stored = readStorage(THEME_STORAGE_KEY)
  if (stored === 'dark' || stored === 'light') return stored
  if (typeof window !== 'undefined' && window.matchMedia?.('(prefers-color-scheme: dark)').matches) return 'dark'
  return 'light'
}

export const useSettingsStore = defineStore('settings', () => {
  /** 是否开启高对比风险高亮 */
  const highlightRisk = ref(true)
  /** 主题模式 */
  const theme = ref<ThemeMode>(initialTheme())
  /** 紧凑表格模式 */
  const compactTable = ref(readStorage(`${THEME_STORAGE_KEY}:compact`) === '1')

  /** 是否为深色主题 */
  const isDark = computed(() => theme.value === 'dark')

  /** 把主题落到 documentElement（CSS 令牌切换）与图表主题 */
  function applyTheme(): void {
    const root = document.documentElement
    root.dataset.theme = theme.value
    root.style.colorScheme = theme.value
    applyChartTheme(isDark.value)
  }

  function setTheme(mode: ThemeMode): void {
    theme.value = mode
    writeStorage(THEME_STORAGE_KEY, mode)
    applyTheme()
  }

  function toggleTheme(): void {
    setTheme(isDark.value ? 'light' : 'dark')
  }

  function toggleCompact(value: boolean): void {
    compactTable.value = value
    writeStorage(`${THEME_STORAGE_KEY}:compact`, value ? '1' : '0')
  }

  // 首次进入立即生效，避免首屏闪烁（index.html 已设置默认底色）
  applyTheme()

  return {
    highlightRisk,
    compactTable,
    theme,
    isDark,
    toggleCompact,
    setTheme,
    toggleTheme,
    applyTheme,
  }
})

/**
 * 通知中心 Store：集中承接「平台级事件」并在本地持久化。
 *
 * 事件来源：
 *  - WebSocket 推送的计划变更（其他会话 / 其他端触发的重规划、删除）；
 *  - 本端写操作结果（新建 / 重规划 / 任务增删改 / 批量编辑）；
 *  - 导入导出、主题切换等功能性动作的轻量回执。
 *
 * 说明：通知**只服务于交互提示与追踪**，不承载任何业务口径，也不替代审计日志
 * （全链路审计由后端 `/api/audit` 提供，通知中心是它的「人读摘要」）。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

/** 通知级别（复用 Element Plus 语义色） */
export type NotifyLevel = 'info' | 'success' | 'warning' | 'danger'

/** 通知条目 */
export interface NotifyItem {
  id: string
  ts: number
  level: NotifyLevel
  title: string
  detail?: string
  /** 关联计划（用于点击跳转） */
  planId?: string
  /** 深链路由名（为空则跳转计划总览） */
  routeName?: string
  read: boolean
}

const STORAGE_KEY = 'iteration-planner:notifications'
/** 本地保留上限，避免无界增长 */
const MAX_ITEMS = 100

function readStored(): NotifyItem[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw) as unknown
    if (!Array.isArray(parsed)) return []
    return parsed.filter((item): item is NotifyItem => {
      const candidate = item as Partial<NotifyItem>
      return typeof candidate?.id === 'string' && typeof candidate?.title === 'string' && typeof candidate?.ts === 'number'
    })
  } catch {
    return []
  }
}

function writeStored(items: NotifyItem[]): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(items.slice(0, MAX_ITEMS)))
  } catch {
    // 忽略存储异常（隐私模式）
  }
}

/** 生成条目 ID（无需加密强度，仅用于列表 key 与去重） */
function newId(): string {
  return `N-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
}

export const useNotifyStore = defineStore('notify', () => {
  const items = ref<NotifyItem[]>(readStored())

  /** 未读数量（顶部铃铛角标） */
  const unread = computed(() => items.value.filter((item) => !item.read).length)

  /** 按时间倒序的列表 */
  const list = computed(() => [...items.value].sort((a, b) => b.ts - a.ts))

  function persist(): void {
    writeStored(items.value)
  }

  /** 追加一条通知（同时落盘） */
  function push(payload: Omit<NotifyItem, 'id' | 'ts' | 'read'> & { ts?: number }): NotifyItem {
    const item: NotifyItem = { id: newId(), ts: payload.ts ?? Date.now(), read: false, ...payload }
    items.value = [item, ...items.value].slice(0, MAX_ITEMS)
    persist()
    return item
  }

  function markRead(id: string): void {
    const target = items.value.find((item) => item.id === id)
    if (!target || target.read) return
    target.read = true
    persist()
  }

  function markAllRead(): void {
    let changed = false
    for (const item of items.value) {
      if (!item.read) {
        item.read = true
        changed = true
      }
    }
    if (changed) persist()
  }

  function remove(id: string): void {
    items.value = items.value.filter((item) => item.id !== id)
    persist()
  }

  function clear(): void {
    items.value = []
    persist()
  }

  return { items, list, unread, push, markRead, markAllRead, remove, clear }
})

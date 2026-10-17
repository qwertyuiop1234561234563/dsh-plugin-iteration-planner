/**
 * 鉴权 Store：登录态、角色与权限清单的唯一前端来源。
 *
 * 设计要点：
 *  - **不在本地存放任何凭据**：登录成功后服务端下发 httpOnly 会话 Cookie（JS 不可读），
 *    页面刷新时通过 `GET /api/auth/session` 复原登录态（服务端为唯一事实来源）；
 *  - **前端权限只做展示收敛**：`can(permission)` 决定入口是否可见，但真正的拦截一律在
 *    服务端完成（本地篡改 state 不会获得任何权限）；
 *  - `401` 由 `client.ts` 的响应拦截器统一触发 {@link bindUnauthorizedHandler}，
 *    清空本地用户态并跳转登录页，避免各页面重复处理会话失效。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api, setUnauthorizedHandler } from '@/api/client'
import type { AuthPermission, AuthRole, AuthUser } from '@/api/types'

/** 角色中文名（与后端 app/constants.py 的 ROLE_LABELS 一致） */
export const ROLE_LABELS: Record<AuthRole, string> = {
  admin: '管理员',
  member: '成员',
  viewer: '只读',
  guest: '访客',
}

/** 角色标签色（Element Plus tag type） */
export const ROLE_TAG_TYPES: Record<AuthRole, 'success' | 'primary' | 'warning' | 'info'> = {
  admin: 'success',
  member: 'primary',
  viewer: 'info',
  guest: 'warning',
}

export const useAuthStore = defineStore('auth', () => {
  /** 当前登录主体（未登录 / 尚未复原时为 null） */
  const user = ref<AuthUser | null>(null)
  /** 是否已完成首次会话复原（避免守卫在复原前误判为未登录） */
  const ready = ref(false)

  /** 是否已登录 */
  const isAuthenticated = computed(() => user.value !== null)
  /** 当前角色（未登录为 null） */
  const role = computed<AuthRole | null>(() => user.value?.role ?? null)
  /** 展示名（优先显示名，其次用户名） */
  const displayName = computed(() => user.value?.displayName || user.value?.username || '')
  /** 当前角色的权限清单 */
  const permissions = computed<AuthPermission[]>(() => user.value?.permissions ?? [])
  /** 是否为访客（计划可见范围受限） */
  const isGuest = computed(() => user.value?.planScope === 'assigned')
  /** 访客可见的计划白名单（其余角色为空） */
  const visiblePlanIds = computed<string[]>(() => user.value?.planIds ?? [])

  /** 是否具备指定权限（前端仅用于展示收敛；服务端仍会独立判定） */
  function can(permission: AuthPermission): boolean {
    return user.value?.permissions.includes(permission) ?? false
  }

  /** 是否具备给定权限中的任意一项 */
  function canAny(list: AuthPermission[]): boolean {
    return list.some((item) => can(item))
  }

  /** 该计划对当前用户是否可见（访客仅限被指派计划） */
  function canSeePlan(planId: string): boolean {
    if (!user.value) return false
    if (user.value.planScope !== 'assigned') return true
    return user.value.planIds.includes(planId)
  }

  /** 账号口令登录 */
  async function login(username: string, password: string): Promise<void> {
    user.value = await api.login(username, password)
    ready.value = true
  }

  /** 注销当前会话（服务端即时吊销，本地立即清空） */
  async function logout(): Promise<void> {
    try {
      await api.logout()
    } finally {
      user.value = null
      ready.value = true
    }
  }

  /** 查询当前登录主体（未登录时返回 null，不抛异常） */
  async function refresh(): Promise<AuthUser | null> {
    try {
      user.value = await api.session()
    } catch {
      // 401（未登录 / 会话过期）与网络不可达都降级为「未登录」，交由登录页统一提示，
      // 避免在路由守卫里抛出未捕获异常导致白屏
      user.value = null
    } finally {
      ready.value = true
    }
    return user.value
  }

  /** 处理会话失效：清空本地用户态（跳转由注册的处理器负责） */
  function clear(): void {
    user.value = null
  }

  /** 在 `client.ts` 中注册 401 处理器，避免每个页面各自处理会话过期 */
  function bindUnauthorizedHandler(handler: () => void): void {
    setUnauthorizedHandler(handler)
  }

  return {
    user,
    ready,
    isAuthenticated,
    role,
    displayName,
    permissions,
    isGuest,
    visiblePlanIds,
    can,
    canAny,
    canSeePlan,
    login,
    logout,
    refresh,
    clear,
    bindUnauthorizedHandler,
  }
})

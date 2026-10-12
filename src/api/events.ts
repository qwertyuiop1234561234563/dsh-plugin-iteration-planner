/**
 * 计划变更事件总线：把「新增 / 修改 / 删除任务、重规划」等变更实时推送给前端（WebSocket）。
 *
 * 采用极简发布订阅，避免为单一用途引入消息中间件；
 * 后续若需要多实例横向扩展，可将其替换为 Redis Pub/Sub，接口保持不变。
 */

import type { ProjectPlan } from '../models/entities.js'

/** 事件类型 */
export type PlanEventType = 'plan.created' | 'plan.updated' | 'plan.deleted'

/** 推送事件载荷 */
export interface PlanEvent {
  type: PlanEventType
  planId: string
  /** ISO 时间字符串 */
  at: string
  /** 触发原因（例如 task_update / replan_all） */
  reason?: string
  /** 变更后的最新计划（plan.deleted 时为空） */
  plan?: ProjectPlan
}

export type PlanEventListener = (event: PlanEvent) => void

/** 计划事件总线 */
export class PlanEventBus {
  private readonly listeners = new Set<PlanEventListener>()

  /** 订阅事件，返回取消订阅函数 */
  subscribe(listener: PlanEventListener): () => void {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  /** 发布事件；单个订阅者异常不影响其它订阅者 */
  publish(event: PlanEvent): void {
    for (const listener of this.listeners) {
      try {
        listener(event)
      } catch (error) {
        process.stderr.write(
          `[iteration-planner/api] 事件订阅者异常：${error instanceof Error ? error.message : String(error)}\n`,
        )
      }
    }
  }

  /** 当前订阅者数量（用于健康检查 / 调试） */
  get size(): number {
    return this.listeners.size
  }
}
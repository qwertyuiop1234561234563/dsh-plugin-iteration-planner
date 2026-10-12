/**
 * 模型实体层（§二 数据模型定义）。
 *
 * 该层只描述数据结构与纯函数工厂，不含任何算法或 IO，供上层的算法层、服务层复用。
 * 字段严格对齐需求 §二，不删减、不改名。
 */

import { randomUUID } from 'node:crypto'
import {
  DEFAULTS,
  TaskStatus,
  TaskType,
  type Priority,
  type RiskLevel,
} from '../constants/enums.js'

/* -------------------------------------------------------------------------- */
/*                                   Task                                     */
/* -------------------------------------------------------------------------- */

/**
 * 任务的“基础字段”——即 LLM 拆解阶段产出的原始任务（§四.1 note：不含 es/ls/slack）。
 */
export interface TaskBase {
  /** 全局唯一 ID（uuid） */
  taskId: string
  /** 任务名称 */
  taskName: string
  /** 详细任务描述 */
  taskDesc: string
  /** 任务类型 */
  taskType: TaskType
  /** 预估工时（小时） */
  estimatedHours: number
  /** 前置依赖 taskId 数组 */
  predecessorTaskIds: string[]
  /** 任务状态 */
  status: TaskStatus
  /** 优先级 */
  priority: Priority
  /** 负责人（可选） */
  assignee?: string
  /** 创建时间戳 */
  createTime: number
  /** 更新时间戳 */
  updateTime: number
}

/**
 * 完整任务实体（§二.1）。
 * es/ls/slack 由 AOE 算法层计算后写入；创建时缺省为 0。
 */
export interface Task extends TaskBase {
  /** 最早开始时间（小时，相对迭代起点） */
  es: number
  /** 最晚开始时间（小时，相对迭代起点） */
  ls: number
  /** 松弛时间 = ls - es；slack=0 代表关键路径任务 */
  slack: number
}

/* -------------------------------------------------------------------------- */
/*                                 RiskItem                                   */
/* -------------------------------------------------------------------------- */

/** 风险实体（§二.3） */
export interface RiskItem {
  /** 风险唯一 ID */
  riskId: string
  /** 风险等级 */
  level: RiskLevel
  /** 关联的任务 ID 数组 */
  taskIds: string[]
  /** 风险描述 */
  description: string
  /** 整改建议 */
  suggest: string
}

/* -------------------------------------------------------------------------- */
/*                               BurndownItem                                  */
/* -------------------------------------------------------------------------- */

/** 燃尽图数据点（§二.4） */
export interface BurndownItem {
  /** 时间节点（ISO 日期字符串） */
  time: string
  /** 剩余未完成工时 */
  remainingHours: number
}

/* -------------------------------------------------------------------------- */
/*                           全局约束 / 项目总计划                              */
/* -------------------------------------------------------------------------- */

/** 全局约束：迭代截止日期、起始日期与开发人数（§四.9 replan_all 入参） */
export interface GlobalConstraints {
  /** 迭代截止日期，ISO 格式 */
  iterationDeadline: string
  /**
   * 迭代起始日期，ISO 格式。
   * 补充字段（已与需求方确认）：用于将任务 es 窗口映射到自然日历，从而支撑“单日过载”校验。
   * 缺省时按 {@link DEFAULTS.START_DATE_SOURCE} 自动取当前日期。
   */
  iterationStartDate?: string
  /** 可用开发人数 */
  devMemberCount: number
}

/** 项目总计划实体（§二.2） */
export interface ProjectPlan {
  /** 计划唯一 ID */
  planId: string
  /** 用户原始需求 */
  rawRequirement: string
  /** 迭代截止日期，ISO 格式 */
  iterationDeadline: string
  /** 迭代起始日期，ISO 格式（补充字段，见 {@link GlobalConstraints.iterationStartDate}） */
  iterationStartDate: string
  /** 可用开发人数 */
  devMemberCount: number
  /** 全部任务集合 */
  taskList: Task[]
  /** 风险列表 */
  riskList: RiskItem[]
  /** 关键路径 taskId 有序数组 */
  criticalPath: string[]
  /** 燃尽图原始数据 */
  burndownRawData: BurndownItem[]
  /** 项目总预估工时 */
  totalEstimatedHours: number
  /** 关键路径总工期（小时） */
  totalDurationHours: number
}

/* -------------------------------------------------------------------------- */
/*                                  工厂函数                                    */
/* -------------------------------------------------------------------------- */

/** 生成全局唯一 ID（uuid v4） */
export function newId(): string {
  return randomUUID()
}

/** 生成风险项 ID，形如 RISK-xxxxxxxx，便于日志与报告识别 */
export function newRiskId(): string {
  return `RISK-${randomUUID().slice(0, 8)}`
}

/**
 * 将 LLM 产出的原始任务规范化为完整 {@link Task} 实体。
 * 补齐 createTime/updateTime/status/assignee，并把 es/ls/slack 初始化为 0（等待算法层计算）。
 */
export function toTask(base: Partial<TaskBase> & Pick<TaskBase, 'taskName'>, now = Date.now()): Task {
  return {
    taskId: base.taskId && base.taskId.length > 0 ? base.taskId : newId(),
    taskName: base.taskName,
    taskDesc: base.taskDesc ?? '',
    taskType: base.taskType ?? TaskType.BUSINESS_CODE,
    estimatedHours: typeof base.estimatedHours === 'number' && base.estimatedHours > 0 ? base.estimatedHours : 1,
    predecessorTaskIds: Array.isArray(base.predecessorTaskIds) ? [...base.predecessorTaskIds] : [],
    status: base.status ?? TaskStatus.TODO,
    priority: base.priority ?? 'P2',
    assignee: base.assignee,
    createTime: base.createTime ?? now,
    updateTime: base.updateTime ?? now,
    // 计算字段：由 AOE 算法层填充，创建阶段统一置 0
    es: 0,
    ls: 0,
    slack: 0,
  }
}

/**
 * 拆解阶段的原始任务工厂（不含 es/ls/slack）。
 * 用于 task_decompose 工具返回，符合 §四.1「仅基础字段」的要求。
 */
export function toTaskBase(base: Partial<TaskBase> & Pick<TaskBase, 'taskName'>, now = Date.now()): TaskBase {
  const { es: _es, ls: _ls, slack: _slack, ...task } = toTask(base, now)
  return task
}

/** 深拷贝任务，避免算法层就地修改调用方对象 */
export function cloneTask(task: Task): Task {
  return { ...task, predecessorTaskIds: [...task.predecessorTaskIds] }
}

/** 从任务列表提取某列字段，用于报表 / 审计 */
export function isEmptyPredecessor(task: Task): boolean {
  return task.predecessorTaskIds.length === 0
}

/** 负责人展示名（缺省使用统一占位，避免报表出现 undefined） */
export function assigneeLabel(task: Task): string {
  return task.assignee && task.assignee.trim().length > 0 ? task.assignee : DEFAULTS.ASSIGNEE
}
/**
 * 常量与枚举层。
 *
 * 规范约束（§7.3）：所有枚举值统一在此文件管理，禁止在业务代码中散落硬编码字符串。
 * 本文件不依赖任何 Harness / Cordis 运行时，可被算法层、服务层、工具层共同引用。
 */

/* -------------------------------------------------------------------------- */
/*                                 任务类型                                    */
/* -------------------------------------------------------------------------- */

/** 任务类型枚举（§二.1 taskType） */
export const TaskType = {
  /** 数据库设计 */
  DB_DESIGN: 'DB_DESIGN',
  /** 接口开发 */
  API_DEV: 'API_DEV',
  /** 业务代码开发 */
  BUSINESS_CODE: 'BUSINESS_CODE',
  /** 联调 / 调试 */
  DEBUG: 'DEBUG',
  /** 测试 */
  TEST: 'TEST',
  /** 部署 / 发布 */
  DEPLOY: 'DEPLOY',
  /** 文档 */
  DOC: 'DOC',
} as const
export type TaskType = (typeof TaskType)[keyof typeof TaskType]
export const TASK_TYPE_VALUES: readonly TaskType[] = Object.values(TaskType)

/* -------------------------------------------------------------------------- */
/*                                 任务状态                                    */
/* -------------------------------------------------------------------------- */

/** 任务状态枚举（§二.1 status） */
export const TaskStatus = {
  /** 待办 */
  TODO: 'TODO',
  /** 进行中 */
  IN_PROGRESS: 'IN_PROGRESS',
  /** 阻塞 */
  BLOCKED: 'BLOCKED',
  /** 测试中 */
  TESTING: 'TESTING',
  /** 已完成 */
  DONE: 'DONE',
  /** 已延期 */
  DELAYED: 'DELAYED',
} as const
export type TaskStatus = (typeof TaskStatus)[keyof typeof TaskStatus]
export const TASK_STATUS_VALUES: readonly TaskStatus[] = Object.values(TaskStatus)

/* -------------------------------------------------------------------------- */
/*                                 优先级                                      */
/* -------------------------------------------------------------------------- */

/** 优先级枚举（§二.1 priority），P0 最高 */
export const Priority = {
  P0: 'P0',
  P1: 'P1',
  P2: 'P2',
  P3: 'P3',
} as const
export type Priority = (typeof Priority)[keyof typeof Priority]
export const PRIORITY_VALUES: readonly Priority[] = Object.values(Priority)

/** 优先级高低顺序（索引越小越高），用于排序与默认负责人分配 */
export const PRIORITY_ORDER: readonly Priority[] = [Priority.P0, Priority.P1, Priority.P2, Priority.P3]

/* -------------------------------------------------------------------------- */
/*                                 风险等级                                    */
/* -------------------------------------------------------------------------- */

/** 风险等级枚举（§二.3 level） */
export const RiskLevel = {
  HIGH: 'HIGH',
  MEDIUM: 'MEDIUM',
  LOW: 'LOW',
} as const
export type RiskLevel = (typeof RiskLevel)[keyof typeof RiskLevel]

/* -------------------------------------------------------------------------- */
/*                              调度 / 排期常量                                 */
/* -------------------------------------------------------------------------- */

export const SCHEDULE = {
  /** 单开发人单日最大工时（小时），用于人员过载校验（§3.3.2 / §4.2） */
  WORK_HOURS_PER_DAY: 8,
  /** 浮点比较容差，用于 slack==0 判定 */
  EPSILON: 1e-6,
  /** 未提供迭代起始日期时，默认从“今天”起算的自然天数（仅用于燃尽图兜底） */
  DEFAULT_ITERATION_DAYS: 10,
} as const

/* -------------------------------------------------------------------------- */
/*                              工时缓冲修正规则                                 */
/* -------------------------------------------------------------------------- */

/**
 * 缓冲规则匹配维度。
 * - type：按任务类型匹配
 * - priority：按优先级匹配
 */
export interface BufferRule {
  /** 规则唯一 key，便于审计日志记录命中的规则 */
  readonly key: string
  /** 规则说明 */
  readonly label: string
  /** 匹配的任务类型（可选） */
  readonly taskType?: TaskType
  /** 匹配的优先级（可选） */
  readonly priority?: Priority
  /** 上浮比例，例如 0.1 表示 +10% */
  readonly ratio: number
}

/**
 * 工时缓冲修正规则表（§3.3）。
 *
 * 修正后的口径（已与需求方确认）：
 * - 多条规则可同时命中同一任务（例如“P0 的 TEST 任务”同时命中 P0 与 TEST）；
 * - 叠加方式为 **按固定顺序相乘**，顺序由 {@link BUFFER_APPLY_ORDER} 决定；
 * - 该顺序与比例均可在插件配置中覆盖，业务代码不写死。
 */
export const BUFFER_RULES: readonly BufferRule[] = [
  { key: 'TYPE_DB_DESIGN', label: 'DB_DESIGN 类型增加 15% 缓冲', taskType: TaskType.DB_DESIGN, ratio: 0.15 },
  { key: 'TYPE_TEST', label: 'TEST 类型增加 20% 缓冲', taskType: TaskType.TEST, ratio: 0.2 },
  { key: 'TYPE_DEPLOY', label: 'DEPLOY 类型增加 20% 缓冲', taskType: TaskType.DEPLOY, ratio: 0.2 },
  { key: 'PRIORITY_P0', label: 'P0 优先级增加 10% 风险缓冲', priority: Priority.P0, ratio: 0.1 },
]

/**
 * 缓冲叠加顺序（已与需求方确认）：先按类型缓冲，再按优先级缓冲，依次相乘。
 * 例：P0 的 TEST 任务 → hours * (1+0.2) * (1+0.1)。
 */
export const BUFFER_APPLY_ORDER: readonly ('type' | 'priority')[] = ['type', 'priority']

/* -------------------------------------------------------------------------- */
/*                                风险规则常量                                  */
/* -------------------------------------------------------------------------- */

export const RISK_RULE = {
  /** 循环依赖风险（§4.1） */
  CYCLE: { code: 'CYCLE_DEPENDENCY', level: RiskLevel.HIGH },
  /** 人员过载风险（§4.2） */
  OVERLOAD: { code: 'MEMBER_OVERLOAD', level: RiskLevel.MEDIUM },
  /** 关键路径延期风险（§4.3） */
  CRITICAL_DELAY: { code: 'CRITICAL_PATH_DELAY', level: RiskLevel.HIGH },
  /** 无前置依赖孤立任务（§4.4） */
  ISOLATED: { code: 'ISOLATED_TASK', level: RiskLevel.LOW },
  /** LLM 语义扫描（§4 尾部） */
  SEMANTIC: { code: 'SEMANTIC_SCAN', level: RiskLevel.MEDIUM },
} as const

/* -------------------------------------------------------------------------- */
/*                                日志 / 审计                                  */
/* -------------------------------------------------------------------------- */

export const AUDIT = {
  /** 审计日志统一前缀 */
  PREFIX: '[iteration-planner/audit]',
  /** 审计日志中单个字符串值截断长度 */
  MAX_STRING_LEN: 4000,
} as const

/* -------------------------------------------------------------------------- */
/*                                计划默认值                                   */
/* -------------------------------------------------------------------------- */

/** 未知任务元数据的默认值集中定义，避免散落魔法值 */
export const DEFAULTS = {
  /** 规划任务时的默认未指派负责人 */
  ASSIGNEE: '待分配',
  /** 未提供迭代起始日期时使用的占位说明 */
  START_DATE_SOURCE: 'auto',
} as const
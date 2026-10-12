/**
 * 规则降级实现：无 LLM / 无 API Key / 脱离 Harness 时使用。
 *
 * 作用（已与需求方确认）：
 *  - 保证本地 Demo 与单元测试可离线、可重复运行；
 *  - LLM 调用失败或输出格式错误时作为兜底路径。
 * 该实现是「确定性」的：相同需求产出相同任务结构。
 */

import { Priority, RiskLevel, TaskStatus, TaskType } from '../constants/enums.js'
import { newId, newRiskId, type RiskItem, type Task, type TaskBase } from '../models/entities.js'
import type { DecomposeInput, SemanticRiskInput, SemanticRiskScanner, TaskDecomposer } from './types.js'

/** 依据需求关键词识别的业务模块 */
const MODULE_KEYWORDS: Array<{ module: string; pattern: RegExp }> = [
  { module: '用户认证', pattern: /(登录|认证|鉴权|账号|用户中心|注册|SSO|token)/i },
  { module: '订单', pattern: /(订单|下单|交易|结算)/i },
  { module: '支付', pattern: /(支付|收银|退款|对账)/i },
  { module: '库存', pattern: /(库存|仓储|扣减|SKU)/i },
  { module: '权限', pattern: /(权限|角色|RBAC|菜单)/i },
  { module: '消息通知', pattern: /(消息|通知|短信|推送|邮件)/i },
  { module: '报表', pattern: /(报表|统计|看板|BI|大盘)/i },
  { module: '搜索', pattern: /(搜索|检索|ES|Elasticsearch)/i },
  { module: '商品', pattern: /(商品|产品|类目)/i },
]

/** 拆解出的模块上限，避免规则兜底产出过长任务链 */
const MAX_MODULES = 3

/** 单个模块的任务骨架（类型 / 名称后缀 / 基础工时 / 优先级） */
const MODULE_SKELETON: Array<{ type: TaskType; suffix: string; hours: number; priority: Priority }> = [
  { type: TaskType.DB_DESIGN, suffix: '数据库设计', hours: 6, priority: Priority.P0 },
  { type: TaskType.API_DEV, suffix: '接口开发', hours: 12, priority: Priority.P0 },
  { type: TaskType.BUSINESS_CODE, suffix: '业务代码实现', hours: 16, priority: Priority.P1 },
  { type: TaskType.DEBUG, suffix: '联调与自测', hours: 6, priority: Priority.P1 },
  { type: TaskType.TEST, suffix: '功能测试', hours: 8, priority: Priority.P1 },
]

/** 构造开发人员池，用于规则分配负责人 */
function buildMemberPool(devCount: number): string[] {
  const count = Number.isFinite(devCount) && devCount > 0 ? Math.floor(devCount) : 1
  return Array.from({ length: count }, (_, i) => `dev-${i + 1}`)
}

/** 识别需求涉及的模块 */
function detectModules(requirement: string): string[] {
  const hit = MODULE_KEYWORDS.filter((k) => k.pattern.test(requirement)).map((k) => k.module)
  const unique = Array.from(new Set(hit))
  return unique.length > 0 ? unique.slice(0, MAX_MODULES) : ['核心业务']
}

/**
 * 规则版需求拆解器。
 * 每个模块产出 DB→API→业务→联调→测试 的线性链，最后统一挂 DEPLOY 与 DOC 汇聚任务。
 */
export class RuleBasedDecomposer implements TaskDecomposer {
  readonly kind = 'rule' as const

  async decompose(input: DecomposeInput): Promise<TaskBase[]> {
    const members = buildMemberPool(input.devCount)
    const modules = detectModules(input.requirement)
    const tasks: TaskBase[] = []
    const now = Date.now()
    let memberCursor = 0

    const takeMember = (): string => {
      const member = members[memberCursor % members.length]
      memberCursor += 1
      return member
    }

    const allTestIds: string[] = []
    const allDevIds: string[] = []

    for (const module of modules) {
      let prevId: string | undefined
      for (const step of MODULE_SKELETON) {
        const taskId = newId()
        tasks.push({
          taskId,
          taskName: `${module}-${step.suffix}`,
          taskDesc: `规则拆解：${module} 的${step.suffix}。来源需求：${input.requirement.slice(0, 80)}`,
          taskType: step.type,
          estimatedHours: step.hours,
          predecessorTaskIds: prevId ? [prevId] : [],
          status: TaskStatus.TODO,
          priority: step.priority,
          assignee: takeMember(),
          createTime: now,
          updateTime: now,
        })
        prevId = taskId
        if (step.type === TaskType.TEST) allTestIds.push(taskId)
        if (step.type === TaskType.API_DEV || step.type === TaskType.BUSINESS_CODE) allDevIds.push(taskId)
      }
    }

    // 汇聚型 DEPLOY 任务：依赖全部测试任务
    const deployId = newId()
    tasks.push({
      taskId: deployId,
      taskName: '版本部署与发布',
      taskDesc: '汇总全部模块，执行部署与发布（依赖所有测试任务）',
      taskType: TaskType.DEPLOY,
      estimatedHours: 4,
      predecessorTaskIds: [...allTestIds],
      status: TaskStatus.TODO,
      priority: Priority.P0,
      assignee: takeMember(),
      createTime: now,
      updateTime: now,
    })

    // 汇聚型 DOC 任务：依赖全部开发任务
    tasks.push({
      taskId: newId(),
      taskName: '发布文档与交付说明',
      taskDesc: '整理接口文档、部署说明与交付物（依赖全部开发任务）',
      taskType: TaskType.DOC,
      estimatedHours: 4,
      predecessorTaskIds: [...allDevIds],
      status: TaskStatus.TODO,
      priority: Priority.P3,
      assignee: takeMember(),
      createTime: now,
      updateTime: now,
    })

    return tasks
  }
}

/** 规则版语义风险扫描：按关键词命中非结构化风险（§4 尾部） */
export class RuleBasedRiskScanner implements SemanticRiskScanner {
  readonly kind = 'rule' as const

  async scan(input: SemanticRiskInput): Promise<RiskItem[]> {
    const { requirement, taskList } = input
    const risks: RiskItem[] = []
    const text = requirement

    const add = (description: string, suggest: string, level: RiskItem['level'], pattern: RegExp): void => {
      if (!pattern.test(text)) return
      const taskIds: string[] = taskList
        .filter((t: Task) => pattern.test(`${t.taskName} ${t.taskDesc}`))
        .map((t) => t.taskId)
      risks.push({ riskId: newRiskId(), level, taskIds, description, suggest })
    }

    add(
      '需求涉及第三方/外部接口对接，外部依赖可能未就绪',
      '提前与第三方确认接口文档、联调窗口与沙箱环境，必要时准备 Mock 降级方案',
      RiskLevel.MEDIUM,
      /(第三方|外部接口|开放平台|对接|SDK|外部服务)/i,
    )
    add(
      '需求可能需要专用测试/联调环境，环境搭建存在不确定性',
      '提前申请或搭建测试环境，纳入迭代前置任务并预留缓冲',
      RiskLevel.MEDIUM,
      /(测试环境|预发|联调环境|灰度|压测环境)/i,
    )
    add(
      '需求描述存在模糊表述，任务边界可能与预期不一致',
      '在需求评审阶段明确验收标准与边界条件，避免开发中返工',
      RiskLevel.MEDIUM,
      /(优化|改造|重构|尽快|完善|相关|若干|待定|等$|体验)/i,
    )

    return risks
  }
}
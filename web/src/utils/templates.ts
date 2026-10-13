/**
 * 迭代模板库：把常用的需求结构固化为可复用模板。
 *
 * 组成：
 *  - 内置模板（贴近常见研发迭代形态，随应用分发，不可删除）；
 *  - 自定义模板（用户在新建计划页保存，持久化在 localStorage，可改名 / 删除）。
 *
 * 模板只提供「需求文本 + 迭代窗口 + 开发人数」的初始值，不预置任务与依赖 ——
 * 任务拆解始终由引擎的规划流水线完成，模板不介入任何算法口径。
 */

/** 迭代模板 */
export interface IterationTemplate {
  id: string
  name: string
  description: string
  /** 需求描述（直接填入新建计划表单） */
  requirement: string
  /** 默认开发人数 */
  devMemberCount: number
  /** 迭代窗口长度（自然日，用于生成默认截止日期） */
  windowDays: number
  /** 是否内置（内置模板不可删除） */
  builtin: boolean
}

/** 内置模板：覆盖典型研发迭代形态 */
export const BUILTIN_TEMPLATES: IterationTemplate[] = [
  {
    id: 'tpl-order-center',
    name: '电商订单中心迭代',
    description: '订单创建 / 支付回调 / 库存扣减 / 查询报表，含外部支付网关依赖',
    devMemberCount: 3,
    windowDays: 21,
    builtin: true,
    requirement:
      '为电商中台开发【订单中心】迭代：1) 完成订单创建、支付回调与库存扣减能力；2) 对接第三方支付网关（外部接口，依赖对方文档与沙箱环境）；3) 支持订单查询与报表统计；4) 优化下单链路性能并搭建压测环境；5) 产出接口文档与部署说明。',
  },
  {
    id: 'tpl-payment-gateway',
    name: '支付网关接入',
    description: '多渠道支付接入、对账与退款，外部依赖密集',
    devMemberCount: 2,
    windowDays: 18,
    builtin: true,
    requirement:
      '接入【聚合支付网关】：1) 完成微信 / 支付宝 / 银联三个渠道的下单与回调适配；2) 实现退款与对账文件解析；3) 统一支付状态机与幂等设计；4) 搭建沙箱联调环境并完成异常链路测试；5) 输出接入文档与灰度发布方案。',
  },
  {
    id: 'tpl-data-platform',
    name: '数据中台报表迭代',
    description: '数仓建模、指标口径统一与看板交付',
    devMemberCount: 3,
    windowDays: 24,
    builtin: true,
    requirement:
      '建设【经营分析数据中台】：1) 完成 ODS/DWD/DWS 分层数仓建模；2) 统一核心指标口径（GMV、履约率、留存）；3) 开发离线与实时双链路 ETL；4) 输出经营看板与管理驾驶舱；5) 补充数据质量校验与任务调度监控。',
  },
  {
    id: 'tpl-mobile-release',
    name: '移动端版本迭代',
    description: 'App 双端功能迭代、兼容性适配与灰度发版',
    devMemberCount: 4,
    windowDays: 20,
    builtin: true,
    requirement:
      '发布【移动端 v3.2 版本】：1) 完成后端接口适配与客户端新功能开发；2) 支持新版本 UI 改版与交互走查；3) 覆盖 iOS / Android 主流机型兼容性测试；4) 完成性能专项（启动耗时、包体瘦身）；5) 通过灰度与热修复机制发版上线。',
  },
  {
    id: 'tpl-service-split',
    name: '微服务拆分治理',
    description: '单体拆分、服务边界梳理与联调回归',
    devMemberCount: 5,
    windowDays: 30,
    builtin: true,
    requirement:
      '推进【单体应用微服务化】：1) 梳理领域边界与拆分方案；2) 抽取用户、订单、库存三个独立服务；3) 完成注册发现、配置中心与链路追踪接入；4) 灰度切流并保证双写一致性；5) 完成全链路回归与回滚预案。',
  },
  {
    id: 'tpl-security-hardening',
    name: '安全合规加固',
    description: '渗透整改、越权治理与合规审计',
    devMemberCount: 2,
    windowDays: 15,
    builtin: true,
    requirement:
      '完成【安全合规专项整改】：1) 修复渗透测试报告中的高危漏洞；2) 治理越权访问与敏感数据明文传输；3) 完善登录风控与审计日志留存；4) 补齐数据脱敏与密钥管理；5) 输出整改报告并配合合规复核。',
  },
  {
    id: 'tpl-performance',
    name: '性能优化专项',
    description: '压测建模、瓶颈定位与容量评估',
    devMemberCount: 3,
    windowDays: 15,
    builtin: true,
    requirement:
      '开展【核心链路性能优化专项】：1) 建立压测模型与基线指标；2) 定位数据库慢查询与锁竞争瓶颈；3) 引入缓存与异步化改造；4) 完成容量评估与限流降级预案；5) 输出性能报告与优化后基线对比。',
  },
]

const STORAGE_KEY = 'iteration-planner:templates'

/** 读取自定义模板 */
export function readCustomTemplates(): IterationTemplate[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw) as unknown
    if (!Array.isArray(parsed)) return []
    return parsed
      .filter((item): item is IterationTemplate => {
        const candidate = item as Partial<IterationTemplate>
        return typeof candidate?.id === 'string' && typeof candidate?.name === 'string' && typeof candidate?.requirement === 'string'
      })
      .map((item) => ({ ...item, builtin: false, devMemberCount: item.devMemberCount || 3, windowDays: item.windowDays || 21 }))
  } catch {
    return []
  }
}

/** 写入自定义模板 */
export function writeCustomTemplates(templates: IterationTemplate[]): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(templates.filter((template) => !template.builtin)))
  } catch {
    // 忽略存储异常
  }
}

/** 生成模板 ID */
export function newTemplateId(): string {
  return `tpl-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`
}

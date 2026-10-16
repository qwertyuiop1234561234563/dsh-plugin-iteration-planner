/**
 * AI 助手内置问答库（**规则降级模式**专用）。
 *
 * 当后端未配置 LLM 网关（`/api/agent/health` 返回 `enabled=false`）时，会话式对话不可用。
 * 此时助手降级为「本地规则问答」：按关键词匹配内置条目作答，并可跳转到对应功能页面。
 *
 * 口径说明：条目的算法口径**全部取自算法引擎与需求文档**（Kahn 拓扑 / AOE 关键路径 /
 * 工时缓冲 §3.3 / 风险检测 §3.4），前端不臆造任何数值或规则；本模块只做文案与导航，
 * 不参与任何计算。
 */

/** 规则问答条目 */
export interface FaqEntry {
  id: string
  /** 展示用问题（快捷提问按钮文案） */
  question: string
  /** 命中关键词（小写匹配，任一命中即计分） */
  keywords: string[]
  answer: string
  /** 可跳转的路由名；计划级页面需同时存在当前计划才可跳转 */
  routeName?: string
  /** 是否产品级问答（无当前计划时也作为快捷提问展示） */
  global?: boolean
}

/** 关键词命中答案（含可选跳转） */
export interface FaqAnswer {
  entry: FaqEntry
  answer: string
}

export const AGENT_FAQ: FaqEntry[] = [
  {
    id: 'capability',
    question: '这个平台能做什么？',
    keywords: ['能做什么', '功能', '介绍', '是什么', '你是谁', '怎么用', 'help', '帮助文档'],
    answer:
      '这是「迭代规划控制台」：把业务需求转化为可执行的迭代计划——自动拆解原子任务、按固定规则修正工时、构建依赖 DAG、用 AOE 关键路径排期、扫描项目风险、生成燃尽数据与迭代报表。左侧「计划列表 / 新建计划」可创建计划，计划级页面提供总览、任务、看板、甘特、DAG、日历、仪表盘、度量、建议、情景对比、治理与质量等视图。',
    global: true,
  },
  {
    id: 'new-plan',
    question: '怎么新建一个迭代计划？',
    keywords: ['新建计划', '创建计划', '新建', '起一个计划', '新计划', '生成计划'],
    answer: '打开「新建计划」页，填写需求原文、迭代截止日期与开发人数，即可一次性跑完 拆解 → 工时缓冲 → DAG 校验 → AOE 排期 → 风险扫描 → 燃尽 → 报表。',
    routeName: 'plan-create',
    global: true,
  },
  {
    id: 'critical-path',
    question: '关键路径是怎么算出来的？',
    keywords: ['关键路径', 'aoe', '关键任务', 'slack', '松弛', '瓶颈', '总工期', '工期怎么算'],
    answer:
      '关键路径由 AOE 网计算：每个任务有最早开始 es、最晚开始 ls，松弛 slack = ls − es；松弛为 0 的任务构成关键路径。项目总工期取 max(es + 自身工时)，而不是关键任务工时之和（多条并行关键分支时后者会重复计工）。表达方式为「活动在顶点」。',
    routeName: 'plan-dag',
  },
  {
    id: 'buffer',
    question: '工时缓冲是怎么加的？',
    keywords: ['缓冲', '工时规则', '上浮', '工时修正', 'buffer', '预估工时'],
    answer:
      '工时修正规则（§3.3）：P0 任务上浮 10%；TEST / DEPLOY 任务增加 20%；DB_DESIGN 任务增加 15%。叠加顺序固定为「类型 → 优先级」（乘法交换律，结果稳定，如 P0 的 TEST = 工时 × 1.2 × 1.1）。',
  },
  {
    id: 'risk',
    question: '系统会检测哪些风险？',
    keywords: ['风险', '风险检测', '有哪些风险', '风险类型', '过载', '延期'],
    answer:
      '引擎硬规则四类：循环依赖（HIGH）、人员过载（MEDIUM，同一开发人单日总工时 > 8h）、关键路径延期（HIGH，关键路径任务状态为 DELAYED）、孤立任务（LOW，既无前置也无后置）。另有一次模型语义扫描，识别需求边界模糊、第三方接口未就绪、测试环境未搭建等非结构化风险。',
    routeName: 'plan-registers',
  },
  {
    id: 'cycle',
    question: '循环依赖是怎么发现的？',
    keywords: ['循环依赖', '成环', '环', '拓扑', 'kahn', 'dag 校验', 'dag'],
    answer:
      '依赖关系用 Kahn 拓扑排序校验：能排出稳定顺序即无环；一旦存在环，重规划会**立即终止**并返回环内任务（错误码 CYCLE_DEPENDENCY），需先修正任务依赖再重试。可在「依赖 DAG」页查看图形化结果。',
    routeName: 'plan-dag',
  },
  {
    id: 'replan',
    question: '修改任务后会发生什么？',
    keywords: ['重规划', 'replan', '改工时', '改依赖', '删除任务', '更新顺序'],
    answer:
      '任何写操作（新增 / 删除任务、修改工时 / 前置依赖 / 状态 / 优先级、调整迭代截止日期或开发人数）都会触发全量重规划，执行顺序严格固定：更新任务集合 → Kahn 拓扑校验（有环立即终止）→ 重算 es/ls、关键路径与总工期 → 全量风险扫描 → 重新生成燃尽数据 → 输出完整计划。',
    routeName: 'plan-timeline',
  },
  {
    id: 'import',
    question: '怎么批量导入任务？',
    keywords: ['导入', 'csv', 'json', '批量', '上传'],
    answer:
      '点击顶部「导入数据」（或按 Ctrl / ⌘ + Shift + I）：支持粘贴或上传 CSV / JSON，可追加到当前计划，也可按导入行新建计划。校验失败会按**行号**定位到具体字段。',
    routeName: 'plan-tasks',
  },
  {
    id: 'baseline',
    question: '基线快照和偏差怎么看？',
    keywords: ['基线', 'baseline', '快照', '偏差', '对比基线'],
    answer: '「基线快照与偏差」页可把当前计划固化为基线，并查看其与现状的任务级偏差（新增 / 删除 / 工时与依赖变化），用于评审与变更追溯。',
    routeName: 'plan-baseline',
  },
  {
    id: 'workload',
    question: '人员负荷怎么看？',
    keywords: ['负荷', '资源', '人力', '工时分布', '平衡'],
    answer: '「人员负荷与资源平衡」页按责任人聚合预估工时，高亮引擎判定的过载成员（单人单日 > 8h），并给出可执行的削峰建议。',
    routeName: 'plan-workload',
  },
  {
    id: 'burndown',
    question: '燃尽图怎么看？',
    keywords: ['燃尽', 'burndown', '剩余工时'],
    answer: '「燃尽增强分析」页使用引擎输出的燃尽原始数据（按工作日推进，跳过周末），展示剩余工时曲线与理想线偏差，判断迭代是否按期收敛。',
    routeName: 'plan-burndown',
  },
  {
    id: 'quality',
    question: '交付质量看板包含什么？',
    keywords: ['质量', '缺陷', '返工', '交付'],
    answer: '「交付质量看板」聚合任务状态分布、延期任务、测试与部署类任务占比、变更频次等交付健康度指标，全部来自引擎返回值与审计记录。',
    routeName: 'plan-quality',
  },
  {
    id: 'shortcut',
    question: '有哪些快捷键？',
    keywords: ['快捷键', '操作说明', '按键', 'shortcut'],
    answer:
      'Ctrl / ⌘ + K 全局搜索；Ctrl / ⌘ + Shift + L 切换主题；+ E 导出任务 CSV；+ H 打印报告；+ I 导入数据；+ A 唤起本助手；F1 或 ? 打开快捷键帮助面板。',
    global: true,
  },
  {
    id: 'assistant',
    question: '你这个助手能做什么？',
    keywords: ['助手', '你能做什么', '你能干嘛', 'ai', 'agent', '对话'],
    answer:
      '在配置了 LLM 网关时，我可以基于引擎真实数据回答计划问题（风险、瓶颈、工时分布），并把「加任务 / 改任务 / 删任务 / 重规划」等写操作先列成清单待你确认后再执行。当前未配置模型，我先以内置问答为你介绍平台能力与算法口径。',
    global: true,
  },
  {
    id: 'knowledge-base',
    question: '知识库是做什么的？',
    keywords: ['知识库', 'rag', '检索', '资料', '文档入库', '向量', 'embedding', '语料'],
    answer:
      '「知识库（RAG）」用于把团队规范、架构说明、历史评审等资料入库：支持上传文件（md / txt / csv / json / html / pdf / docx）或直接粘贴文本，服务端按语义边界分块后用向量化模型建索引。对话时采用混合检索（稠密向量 + BM25 → RRF 融合）自动召回相关片段注入上下文，并在回答下方标注引用依据，便于核对结论来源。',
    routeName: 'knowledge',
    global: true,
  },
  {
    id: 'memory',
    question: '长期记忆是怎么工作的？',
    keywords: ['记忆', '记忆功能', '长期记忆', '记住', 'mem0', '会不会忘'],
    answer:
      '「长期记忆」由 Agent 在每轮对话后自动提取：把出现的偏好、决策、约束沉淀为「自包含的一句话」，先与既有记忆做向量相似度去重（阈值 0.92）再入库。下一轮对话按语义相关度 + 重要度召回 top-k 注入上下文，因此「我是这样约定的」能被记住。跑偏的记忆可在该页停用或删除，也可手工补录。',
    routeName: 'memory',
    global: true,
  },
]

/** 无命中时的兜底回答（明确告知能力边界与配置方式） */
export const FAQ_FALLBACK =
  '这个问题我暂时没有内置答案（**当前未配置 LLM 网关，处于规则问答模式**）。你可以试试：关键路径怎么算 / 工时缓冲规则 / 会检测哪些风险 / 怎么新建计划 / 知识库怎么用 / 长期记忆怎么工作 / 有哪些快捷键。\n\n' +
  '若要启用模型问答与「改计划」能力，请在网关环境变量中配置 LLM_BASE_URL、LLM_API_KEY、LLM_MODEL 后重启服务。'

/** 命中得分：关键词出现次数（长关键词权重更高，避免「环」这类短词误命中） */
function score(entry: FaqEntry, text: string): number {
  let total = 0
  for (const keyword of entry.keywords) {
    if (text.includes(keyword)) total += keyword.length >= 3 ? 2 : 1
  }
  return total
}

/** 在问答库中检索最佳答案；无命中返回 ``null``（由调用方给出兜底文案） */
export function matchFaq(text: string): FaqAnswer | null {
  const normalized = text.trim().toLowerCase()
  if (!normalized) return null
  let best: FaqEntry | null = null
  let bestScore = 0
  for (const entry of AGENT_FAQ) {
    const current = score(entry, normalized)
    if (current > bestScore) {
      best = entry
      bestScore = current
    }
  }
  return best ? { entry: best, answer: best.answer } : null
}

/** 规则模式下的快捷提问（有当前计划时优先展示计划级问答） */
export function faqPrompts(hasPlan: boolean, limit = 4): FaqEntry[] {
  const scoped = AGENT_FAQ.filter((entry) => !entry.global && (!entry.routeName || hasPlan))
  const pool = scoped.length >= limit ? scoped : [...scoped, ...AGENT_FAQ.filter((entry) => entry.global)]
  return pool.slice(0, limit)
}

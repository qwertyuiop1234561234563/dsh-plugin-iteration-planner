/**
 * 本地 Demo 入口（§7.7）：输入一个后端业务需求样例，直接运行完整流程。
 *
 * 运行：npm run demo
 *
 * 说明：Demo 直接驱动 {@link PlannerService}（纯 TS 业务服务），
 * 不依赖 Cordis 运行时即可复现「拆解 → 缓冲 → DAG 校验 → AOE 排期 → 风险 → 燃尽 → 报表」全流程；
 * 同时通过 {@link createAllTools} 展示插件向 Harness 注册的 11 个工具定义。
 * 审计日志输出到 stderr，标准输出仅保留业务结果，便于重定向为样例文件。
 */

import { RiskLevel, TaskStatus } from '../src/constants/enums.js'
import { AuditLogger, createConsoleSink } from '../src/logger/audit.js'
import type { ProjectPlan } from '../src/models/entities.js'
import { PlanCycleError, PlannerService } from '../src/service/planner-service.js'
import { buildReport } from '../src/service/report.js'
import { createAllTools } from '../src/tools/index.js'

/* -------------------------------------------------------------------------- */
/*                                  样例输入                                    */
/* -------------------------------------------------------------------------- */

const REQUIREMENT = [
  '为电商中台开发【订单中心 v2.1】迭代：',
  '1) 完成订单创建、支付回调与库存扣减能力；',
  '2) 需对接第三方支付网关（外部接口，依赖对方文档与沙箱环境）；',
  '3) 支持订单查询与报表统计；',
  '4) 优化下单链路性能，并搭建压测环境；',
  '5) 产出接口文档与部署说明。',
].join('')

const ITERATION_START = '2026-10-12'
const ITERATION_DEADLINE = '2026-10-30'
const DEV_COUNT = 3

/* -------------------------------------------------------------------------- */
/*                                  输出工具                                    */
/* -------------------------------------------------------------------------- */

const out = (text = ''): void => {
  process.stdout.write(`${text}\n`)
}

function banner(title: string): void {
  out()
  out('='.repeat(78))
  out(`# ${title}`)
  out('='.repeat(78))
}

function step(title: string): void {
  out()
  out(`--- ${title} ---`)
}

/** 计划的结构化摘要（与完整 JSON 互补，便于快速阅读） */
function summarize(plan: ProjectPlan): Record<string, unknown> {
  const nameOf = (id: string): string => plan.taskList.find((t) => t.taskId === id)?.taskName ?? id
  return {
    planId: plan.planId,
    iterationWindow: `${plan.iterationStartDate} ~ ${plan.iterationDeadline}`,
    devMemberCount: plan.devMemberCount,
    taskCount: plan.taskList.length,
    totalEstimatedHours: plan.totalEstimatedHours,
    totalDurationHours: plan.totalDurationHours,
    criticalPath: plan.criticalPath.map(nameOf),
    riskCountByLevel: {
      HIGH: plan.riskList.filter((r) => r.level === RiskLevel.HIGH).length,
      MEDIUM: plan.riskList.filter((r) => r.level === RiskLevel.MEDIUM).length,
      LOW: plan.riskList.filter((r) => r.level === RiskLevel.LOW).length,
    },
  }
}

function printRisks(plan: ProjectPlan): void {
  if (plan.riskList.length === 0) {
    out('（无风险）')
    return
  }
  for (const risk of plan.riskList) {
    out(`【${risk.level}】${risk.description}`)
    out(`  └ 整改建议：${risk.suggest}`)
  }
}

/* -------------------------------------------------------------------------- */
/*                                   主流程                                     */
/* -------------------------------------------------------------------------- */

async function main(): Promise<void> {
  const audit = new AuditLogger(createConsoleSink())

  // 不注入 llm：演示「规则降级」路径，保证离线可运行且结果可复现。
  // 接入 Harness 后，可在插件配置中提供 llmProvider/llmModel 启用 LLM 主路径。
  const service = new PlannerService({ audit })

  banner('企业级研发迭代规划智能 Agent —— 本地 Demo')
  out('拆解策略：规则实现（离线可复现）。接入 Harness 配置 llmProvider/llmModel 后切换为 LLM 主路径。')
  out(`样例需求：${REQUIREMENT}`)
  out(`迭代窗口：${ITERATION_START} ~ ${ITERATION_DEADLINE}；可用开发人数：${DEV_COUNT}`)

  step('STEP 0 · 插件注册的工具清单（Harness 侧可见 11 个）')
  const tools = createAllTools(service)
  out(tools.map((t) => t.name).join(', '))

  /* ----------------------- STEP 1：完整规划（步骤 2~7） ---------------------- */
  step('STEP 1 · 调用 plan_iteration：拆解 → 缓冲 → DAG → AOE → 风险 → 燃尽 → 报表')
  const plan = await service.createPlan(REQUIREMENT, {
    iterationDeadline: ITERATION_DEADLINE,
    iterationStartDate: ITERATION_START,
    devMemberCount: DEV_COUNT,
  })

  out()
  out('【计划结构化摘要】')
  out(JSON.stringify(summarize(plan), null, 2))

  out()
  out('【工时缓冲修正明细（§3.3）】')
  for (const adj of service.getBufferAdjustments()) {
    out(`- ${adj.taskName}: ${adj.before}h → ${adj.after}h（命中 ${adj.appliedRules.join(', ')}）`)
  }

  out()
  out('【风险清单】')
  printRisks(plan)

  out()
  out('【燃尽图原始数据】')
  out(JSON.stringify(plan.burndownRawData, null, 2))

  out()
  out('【完整 ProjectPlan JSON】')
  out(JSON.stringify(plan, null, 2))

  /* ------------------------- STEP 2：文本报表（§四.10） ---------------------- */
  step('STEP 2 · report_generate：团队可读文本报告')
  out(buildReport(plan).text)

  /* --------------------- STEP 3：动态变更 + 全量重规划 ---------------------- */
  step('STEP 3 · task_update：把关键路径任务标记为 DELAYED，自动触发 replan_all')

  const criticalTaskId = plan.criticalPath[0]
  const criticalTaskName = plan.taskList.find((t) => t.taskId === criticalTaskId)?.taskName ?? criticalTaskId
  out(`目标关键任务：${criticalTaskName}（${criticalTaskId}）→ status=DELAYED`)

  const { plan: replanned } = await service.updateTask(criticalTaskId, 'status', TaskStatus.DELAYED)
  out(`重规划后总工期：${replanned.totalDurationHours}h；风险条目：${replanned.riskList.length}`)
  const delayRisk = replanned.riskList.find(
    (r) => r.level === RiskLevel.HIGH && r.taskIds.includes(criticalTaskId),
  )
  out(delayRisk ? `已高亮 HIGH 风险：${delayRisk.description}` : '（未检出关键路径延期风险，请检查输入）')

  /* --------------------- STEP 4：循环依赖阻断与恢复 ------------------------- */
  step('STEP 4 · 构造循环依赖：验证 dag_check 阻断 + 报错可读性')

  const lastTaskId = plan.taskList[plan.taskList.length - 1].taskId
  const { taskList: afterAdd } = await service.addTask({
    taskName: '循环验证任务',
    taskDesc: '仅用于演示循环依赖阻断',
    predecessorTaskIds: [lastTaskId],
    estimatedHours: 2,
  })
  const added = afterAdd.find((t) => t.taskName === '循环验证任务')!
  out(`新增任务：${added.taskName}（${added.taskId}）依赖 ${lastTaskId}`)
  out(`现在把 ${lastTaskId} 的前置改为 ${added.taskId}，构成 A→B→A 环……`)

  try {
    await service.updateTask(lastTaskId, 'predecessorTaskIds', [added.taskId])
    out('！未按预期检出环，请检查算法实现')
  } catch (error) {
    if (error instanceof PlanCycleError) {
      out(`已按预期阻断：${error.message}`)
      out(`环内任务：${error.cycleNodes.join('、')}`)
    } else {
      throw error
    }
  }

  out('通过 task_delete 移除新增任务以解除环，验证自动恢复：')
  const { plan: recovered } = await service.deleteTask(added.taskId)
  out(`恢复后总工期：${recovered.totalDurationHours}h；风险条目：${recovered.riskList.length}`)

  banner('Demo 结束')
  out('审计日志（含每次工具调用的入参/出参/耗时）已输出到 stderr。')
}

main().catch((error: unknown) => {
  // 异常处理（§8）：任何未捕获异常都要有明确提示，绝不静默崩溃
  process.stderr.write(`\n[DEMO ERROR] ${error instanceof Error ? `${error.name}: ${error.message}` : String(error)}\n`)
  process.exitCode = 1
})
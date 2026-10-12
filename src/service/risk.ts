/**
 * 风险检测（§四.8）：硬编码规则 + LLM 语义扫描的组合。
 *
 * 本文件只负责「硬编码规则」部分（§4.1~§4.4）；
 * 语义扫描由 {@link SemanticRiskScanner} 策略注入，二者结果在服务层合并。
 */

import type { DagCheckResult, DagGraph } from '../algorithm/dag.js'
import { findIsolatedTasks } from '../algorithm/dag.js'
import type { ScheduleResult } from '../algorithm/aoe.js'
import { RISK_RULE, TaskStatus } from '../constants/enums.js'
import { newRiskId, type RiskItem, type Task } from '../models/entities.js'
import type { DailyLoadEntry } from './calendar.js'

/** 硬编码规则入参 */
export interface HardcodedRiskInput {
  taskList: readonly Task[]
  /** 图结构，用于孤立任务判定 */
  graph: DagGraph
  /** Kahn 校验结果，用于循环依赖判定 */
  dag: DagCheckResult
  /** AOE 排期结果，用于关键路径延期判定 */
  schedule: ScheduleResult
  /** 人员单日负载明细，用于过载判定 */
  overloads: readonly DailyLoadEntry[]
}

/**
 * 执行 4 条硬编码风险规则：
 *  1. 循环依赖（HIGH）
 *  2. 人员过载（MEDIUM）
 *  3. 关键路径延期（HIGH）
 *  4. 无前置依赖孤立任务（LOW）
 */
export function runHardcodedRiskRules(input: HardcodedRiskInput): RiskItem[] {
  const { taskList, graph, dag, schedule, overloads } = input
  const risks: RiskItem[] = []

  /* ---------------------- 1. 循环依赖风险（§4.1） ---------------------- */
  if (dag.hasCycle) {
    risks.push({
      riskId: newRiskId(),
      level: RISK_RULE.CYCLE.level,
      taskIds: [...dag.cycleNodes],
      description: `检测到循环依赖，环内任务：${dag.cycleNodes.join('、')}`,
      suggest: '修正前置依赖关系，打破环（例如移除或反转其中一条依赖边）后重新规划',
    })
  }

  /* ----------------------- 2. 人员过载风险（§4.2） ---------------------- */
  const overloadByMember = new Map<string, DailyLoadEntry[]>()
  for (const entry of overloads) {
    const list = overloadByMember.get(entry.assignee) ?? []
    list.push(entry)
    overloadByMember.set(entry.assignee, list)
  }
  for (const [assignee, entries] of overloadByMember) {
    const maxDay = entries.reduce((acc, e) => (e.hours > acc.hours ? e : acc), entries[0])
    const taskIds = Array.from(new Set(entries.flatMap((e) => e.taskIds)))
    risks.push({
      riskId: newRiskId(),
      level: RISK_RULE.OVERLOAD.level,
      taskIds,
      description: `开发人员「${assignee}」存在 ${entries.length} 天单日工时超限，峰值 ${maxDay.hours}h（${maxDay.date}）`,
      suggest: '拆分任务、调整任务并行度或增加人手，确保单人单日总工时不超过 8 小时',
    })
  }

  /* --------------------- 3. 关键路径延期风险（§4.3） -------------------- */
  const criticalSet = new Set(schedule.criticalTaskIds)
  const delayedOnCritical = taskList.filter((t) => criticalSet.has(t.taskId) && t.status === TaskStatus.DELAYED)
  if (delayedOnCritical.length > 0) {
    risks.push({
      riskId: newRiskId(),
      level: RISK_RULE.CRITICAL_DELAY.level,
      taskIds: delayedOnCritical.map((t) => t.taskId),
      description: `关键路径上存在 DELAYED 任务：${delayedOnCritical.map((t) => t.taskName).join('、')}`,
      suggest: '调整排期或增加资源优先保障关键路径任务，必要时重排依赖以压缩工期',
    })
  }

  /* ---------------------- 4. 孤立任务风险（§4.4） ---------------------- */
  const isolated = findIsolatedTasks(taskList, graph)
  if (isolated.length > 0) {
    const names = isolated.map((id) => taskList.find((t) => t.taskId === id)?.taskName ?? id)
    risks.push({
      riskId: newRiskId(),
      level: RISK_RULE.ISOLATED.level,
      taskIds: isolated,
      description: `存在既无前置也无后置的孤立任务：${names.join('、')}`,
      suggest: '确认依赖关系是否遗漏；若确为独立任务可忽略，否则补充前置/后置依赖',
    })
  }

  return risks
}
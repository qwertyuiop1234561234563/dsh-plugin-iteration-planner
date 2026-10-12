/**
 * 工具定义层 —— 风险扫描工具（§四.8 risk_scan）。
 *
 * 组合「硬编码规则（循环依赖 / 人员过载 / 关键路径延期 / 孤立任务）」+「LLM 语义扫描」。
 */

import { defineTool, type ToolDefinition } from '@deepseek-ai/dsh-tools'
import { toTask } from '../models/entities.js'
import { PlanCycleError, type PlannerService } from '../service/planner-service.js'
import { asTaskInput, textBlocks, toJson, TASK_OBJECT_SPEC, toTaskDrafts } from './schemas.js'

export function createRiskTools(service: PlannerService): ToolDefinition[] {
  const riskScan = defineTool({
    name: 'risk_scan',
    description:
      '执行全量风险扫描：硬编码规则（循环依赖/人员过载/关键路径延期/孤立任务）+ LLM 语义风险识别。不传 taskList 时使用当前内存任务集合。',
    parameters: {
      taskList: { type: 'array', items: TASK_OBJECT_SPEC, description: '可选：用于扫描的任务集合' },
      scheduleResult: { type: 'json', description: '可选：排期结果（仅作上下文，内部会按同一任务集合重算以保证一致性）' },
    },
    output: { schema: { type: 'json' }, render: (_args, value) => textBlocks(value) },
    async execute(args) {
      const tasks = args.taskList ? toTaskDrafts(args.taskList).map((draft) => toTask(asTaskInput(draft))) : undefined
      try {
        const riskList = await service.riskScan(tasks)
        return toJson({ ok: true, riskList })
      } catch (error) {
        if (error instanceof PlanCycleError) {
          return toJson({ ok: false, reason: 'CYCLE_DEPENDENCY', message: error.message, cycleNodes: error.cycleNodes })
        }
        throw error
      }
    },
  })

  return [riskScan]
}
/**
 * 工具定义层 —— 规划编排类工具（§四.1 / §四.9 + plan_iteration 高层入口）。
 *
 * 包含：task_decompose、plan_iteration、replan_all。
 * 所有工具仅做「入参 → 调用服务 → 输出 canonical JSON」，不承担算法逻辑。
 */

import { defineTool, type ToolDefinition } from '@deepseek-ai/dsh-tools'
import { buildReport } from '../service/report.js'
import { PlanCycleError, type PlannerService } from '../service/planner-service.js'
import { asTaskInput, textBlocks, toJson, TASK_OBJECT_SPEC, toTaskDrafts } from './schemas.js'

export function createPlanningTools(service: PlannerService): ToolDefinition[] {
  /** §四.1 task_decompose：把大需求拆解为原子研发任务（不含 es/ls/slack） */
  const taskDecompose = defineTool({
    name: 'task_decompose',
    description:
      '调用 LLM 将大需求拆解为原子研发任务，返回基础字段数组（不含 es/ls/slack）。LLM 失败会按配置降级为规则拆解。',
    parameters: {
      requirement: { type: 'string', required: true, description: '原始需求文档 / 版本开发需求' },
      devCount: { type: 'integer', required: true, description: '可用开发人数' },
      deadline: { type: 'string', required: true, description: '迭代截止日期（ISO 格式）' },
    },
    output: { schema: { type: 'json' }, render: (_args, value) => textBlocks(value) },
    async execute(args) {
      const tasks = await service.decomposeRequirement(args.requirement, args.devCount, args.deadline)
      return toJson(tasks)
    },
  })

  /** 高层编排入口：一次性跑完 §五 步骤 2~7，降低模型逐步调用出错的概率 */
  const planIteration = defineTool({
    name: 'plan_iteration',
    description:
      '高层编排入口：接收需求与约束，一次性完成「拆解→工时缓冲→DAG校验→AOE排期→风险扫描→燃尽→报表」。存在循环依赖时返回结构化错误。',
    parameters: {
      requirement: { type: 'string', required: true, description: '原始需求文档 / 版本开发需求' },
      devCount: { type: 'integer', required: true, description: '可用开发人数' },
      deadline: { type: 'string', required: true, description: '迭代截止日期（ISO 格式）' },
      iterationStartDate: { type: 'string', description: '迭代起始日期（ISO 格式，可选，缺省取当天）' },
    },
    output: { schema: { type: 'json' }, render: (_args, value) => textBlocks(value) },
    async execute(args) {
      try {
        const plan = await service.createPlan(args.requirement, {
          iterationDeadline: args.deadline,
          iterationStartDate: args.iterationStartDate,
          devMemberCount: args.devCount,
        })
        const report = buildReport(plan)
        return toJson({ ok: true, plan, reportText: report.text })
      } catch (error) {
        if (error instanceof PlanCycleError) {
          return toJson({
            ok: false,
            reason: 'CYCLE_DEPENDENCY',
            message: error.message,
            cycleNodes: error.cycleNodes,
            hint: '请修正前置依赖关系后重试',
          })
        }
        throw error
      }
    },
  })

  /** §四.9 replan_all：全量重规划（步骤 1~6 严格顺序） */
  const replanAll = defineTool({
    name: 'replan_all',
    description:
      '全量重规划：按「DAG校验→排期→风险扫描→燃尽」的固定顺序重算，返回完整 ProjectPlan。可选传入新的 taskList 与全局约束（迭代截止、起始日期、开发人数）。',
    parameters: {
      taskList: { type: 'array', items: TASK_OBJECT_SPEC, description: '可选：用该任务集合替换当前内存集合' },
      globalConstraints: {
        type: 'object',
        additionalProperties: true,
        description: '全局约束：iterationDeadline / iterationStartDate / devMemberCount',
        properties: {
          iterationDeadline: { type: 'string', description: '迭代截止日期（ISO）' },
          iterationStartDate: { type: 'string', description: '迭代起始日期（ISO）' },
          devMemberCount: { type: 'integer', description: '可用开发人数' },
        },
      },
    },
    output: { schema: { type: 'json' }, render: (_args, value) => textBlocks(value) },
    async execute(args) {
      const rawConstraints = (args.globalConstraints ?? {}) as {
        iterationDeadline?: string
        iterationStartDate?: string
        devMemberCount?: number
      }
      const constraints = {
        iterationDeadline: rawConstraints.iterationDeadline,
        iterationStartDate: rawConstraints.iterationStartDate,
        devMemberCount: rawConstraints.devMemberCount,
      }
      try {
        const plan = args.taskList
          ? await service.replaceTasksAndReplan(toTaskDrafts(args.taskList).map(asTaskInput), constraints)
          : await service.replanAll(constraints)
        return toJson({ ok: true, plan })
      } catch (error) {
        if (error instanceof PlanCycleError) {
          return toJson({ ok: false, reason: 'CYCLE_DEPENDENCY', message: error.message, cycleNodes: error.cycleNodes })
        }
        throw error
      }
    },
  })

  return [taskDecompose, planIteration, replanAll]
}
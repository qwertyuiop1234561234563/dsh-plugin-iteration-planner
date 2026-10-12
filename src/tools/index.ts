/**
 * 工具统一注册出口。
 *
 * @param ctx Harness 上下文
 * @param service 迭代规划服务实例
 */
import type { Context } from '@deepseek-ai/cordis'
import type { ToolDefinition } from '@deepseek-ai/dsh-tools'
import type { PlannerService } from '../service/planner-service.js'
import { createDagTools } from './dag-tools.js'
import { createPlanningTools } from './planning-tools.js'
import { createReportTools } from './report-tools.js'
import { createRiskTools } from './risk-tools.js'
import { createTaskTools } from './task-tools.js'

/** 构造全部工具定义（共 10 个规范工具 + 1 个 plan_iteration 编排入口） */
export function createAllTools(service: PlannerService): ToolDefinition[] {
  return [
    ...createPlanningTools(service), // task_decompose, plan_iteration, replan_all
    ...createDagTools(service), // dag_build, dag_check, schedule_calc
    ...createTaskTools(service), // task_add, task_update, task_delete
    ...createRiskTools(service), // risk_scan
    ...createReportTools(service), // report_generate
  ]
}

/**
 * 注册全部工具到 `ctx.tools`。
 * `ctx.tools.register` 会返回反注册函数并绑定到当前插件的 effect，
 * 插件卸载时工具会自动注销（Cordis 可逆副作用）。
 */
export function registerAllTools(ctx: Context, service: PlannerService): void {
  for (const tool of createAllTools(service)) {
    ctx.tools.register(tool)
  }
}
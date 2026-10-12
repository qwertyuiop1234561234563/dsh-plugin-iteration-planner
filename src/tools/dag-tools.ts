/**
 * 工具定义层 —— 图算法类工具（§四.2 dag_build / §四.3 dag_check / §四.7 schedule_calc）。
 *
 * 说明：这些工具默认作用于「当前内存任务集合」；
 * 也支持显式传入 taskList 做一次性计算（不改变内部状态）。
 */

import { defineTool, type ToolDefinition } from '@deepseek-ai/dsh-tools'
import { buildDag, computeSchedule, kahnCheck } from '../algorithm/index.js'
import { toTask } from '../models/entities.js'
import type { PlannerService } from '../service/planner-service.js'
import { asTaskInput, textBlocks, toJson, TASK_OBJECT_SPEC, toTaskDrafts } from './schemas.js'

export function createDagTools(service: PlannerService): ToolDefinition[] {
  /** §四.2 dag_build：构建邻接表与入度数组 */
  const dagBuild = defineTool({
    name: 'dag_build',
    description: '根据任务列表构建 DAG 图结构，返回邻接表 adjList 与入度数组 inDegree。不传 taskList 时使用当前内存任务集合。',
    parameters: {
      taskList: { type: 'array', items: TASK_OBJECT_SPEC, description: '可选：用于构建图的任务集合' },
    },
    output: { schema: { type: 'json' }, render: (_args, value) => textBlocks(value) },
    async execute(args) {
      const graph = args.taskList
        ? buildDag(toTaskDrafts(args.taskList).map((draft) => toTask(asTaskInput(draft))))
        : service.dagBuild()
      return toJson(graph)
    },
  })

  /** §四.3 dag_check：Kahn 环检测 + 拓扑排序 */
  const dagCheck = defineTool({
    name: 'dag_check',
    description:
      '执行 Kahn 拓扑校验：返回 hasCycle、cycleNodes（环内任务）、topoOrder（拓扑顺序）。可直接传入 adjList/inDegree，或使用当前内存任务集合。',
    parameters: {
      adjList: { type: 'json', description: '可选：邻接表（来自 dag_build）' },
      inDegree: { type: 'json', description: '可选：入度数组（来自 dag_build）' },
    },
    output: { schema: { type: 'json' }, render: (_args, value) => textBlocks(value) },
    async execute(args) {
      if (args.adjList && args.inDegree) {
        const result = kahnCheck({
          adjList: args.adjList as Record<string, string[]>,
          inDegree: args.inDegree as Record<string, number>,
        })
        return toJson(result)
      }
      return toJson(service.dagCheck().result)
    },
  })

  /** §四.7 schedule_calc：AOE 计算关键路径与时间窗口 */
  const scheduleCalc = defineTool({
    name: 'schedule_calc',
    description:
      'AOE 网算法计算关键路径、各任务 es/ls/slack 与总工期。存在循环依赖时返回结构化错误。不传 taskList 时使用当前内存任务集合。',
    parameters: {
      taskList: { type: 'array', items: TASK_OBJECT_SPEC, description: '可选：用于计算的任务集合' },
      topoOrder: { type: 'array', items: { type: 'string' }, description: '可选：拓扑顺序（来自 dag_check）' },
    },
    output: { schema: { type: 'json' }, render: (_args, value) => textBlocks(value) },
    async execute(args) {
      const tasks = args.taskList ? toTaskDrafts(args.taskList).map((draft) => toTask(asTaskInput(draft))) : service.getTaskList()
      const graph = buildDag(tasks)
      const dag = kahnCheck(graph)
      if (dag.hasCycle) {
        return toJson({ ok: false, reason: 'CYCLE_DEPENDENCY', cycleNodes: dag.cycleNodes, message: `检测到循环依赖：${dag.cycleNodes.join('、')}` })
      }
      const schedule = computeSchedule(tasks, args.topoOrder ?? dag.topoOrder)
      return toJson({
        ok: true,
        criticalPath: schedule.criticalPath,
        criticalTaskIds: schedule.criticalTaskIds,
        esMap: schedule.esMap,
        lsMap: schedule.lsMap,
        slackMap: schedule.slackMap,
        totalDurationHours: schedule.totalDurationHours,
      })
    },
  })

  return [dagBuild, dagCheck, scheduleCalc]
}
/**
 * 工具定义层 —— 任务变更类工具（§四.4 task_add / §四.5 task_update / §四.6 task_delete）。
 *
 * 每个工具在变更内存任务集合后，均触发全量重规划（§3.5），返回更新后的完整任务集合。
 * 校验失败（任务不存在、字段非法、依赖不存在）会抛出明确错误，由 Harness 转为模型可见的失败结果。
 */

import { defineTool, type ToolDefinition } from '@deepseek-ai/dsh-tools'
import type { PlannerService, UpdatableField } from '../service/planner-service.js'
import { asTaskInput, textBlocks, toJson, TASK_OBJECT_SPEC, type TaskDraftInput } from './schemas.js'

export function createTaskTools(service: PlannerService): ToolDefinition[] {
  /** §四.4 task_add：新增任务并全量重规划 */
  const taskAdd = defineTool({
    name: 'task_add',
    description: '新增一个任务到任务集合，随后自动触发全量重规划，返回更新后的完整 taskList 与计划摘要。',
    parameters: {
      newTask: { ...TASK_OBJECT_SPEC, required: true, description: '待新增的任务对象' },
    },
    output: { schema: { type: 'json' }, render: (_args, value) => textBlocks(value) },
    async execute(args) {
      const draft = args.newTask as unknown as TaskDraftInput
      const { taskList, plan } = await service.addTask(
        asTaskInput({
          ...draft,
          taskName: draft.taskName,
          predecessorTaskIds: draft.predecessorTaskIds,
        }),
      )
      return toJson({ ok: true, taskList, planId: plan.planId, riskCount: plan.riskList.length, totalDurationHours: plan.totalDurationHours })
    },
  })

  /** §四.5 task_update：修改任务字段并全量重规划 */
  const taskUpdate = defineTool({
    name: 'task_update',
    description:
      '修改指定任务的一个字段（estimatedHours / predecessorTaskIds / status / priority / assignee），随后自动触发全量重规划。',
    parameters: {
      taskId: { type: 'string', required: true, description: '目标任务 ID' },
      updateField: {
        type: 'string',
        required: true,
        enum: ['estimatedHours', 'predecessorTaskIds', 'status', 'priority', 'assignee'],
        description: '要更新的字段名',
      },
      newValue: { type: 'json', required: true, description: '新值；工时=数字，依赖=字符串数组，状态/优先级=枚举字符串' },
    },
    output: { schema: { type: 'json' }, render: (_args, value) => textBlocks(value) },
    async execute(args) {
      // updateField 已由 enum 校验；newValue 的具体类型由服务层做业务校验
      const { task, plan } = await service.updateTask(args.taskId, args.updateField as UpdatableField, args.newValue)
      return toJson({ ok: true, task, planId: plan.planId, criticalPath: plan.criticalPath })
    },
  })

  /** §四.6 task_delete：删除任务并清理引用，随后全量重规划 */
  const taskDelete = defineTool({
    name: 'task_delete',
    description: '删除指定任务，并自动从其他任务的 predecessorTaskIds 中移除该 taskId 引用，随后触发全量重规划。',
    parameters: {
      taskId: { type: 'string', required: true, description: '待删除任务 ID' },
    },
    output: { schema: { type: 'json' }, render: (_args, value) => textBlocks(value) },
    async execute(args) {
      const { taskList, plan } = await service.deleteTask(args.taskId)
      return toJson({ ok: true, taskList, planId: plan.planId, riskCount: plan.riskList.length })
    },
  })

  return [taskAdd, taskUpdate, taskDelete]
}
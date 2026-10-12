/**
 * 工具定义层 —— 报表工具（§四.10 report_generate）。
 *
 * 输出「团队可读文本报告」+「燃尽图原始数据」。
 */

import { defineTool, type ToolDefinition } from '@deepseek-ai/dsh-tools'
import type { ProjectPlan } from '../models/entities.js'
import { buildReport } from '../service/report.js'
import type { PlannerService } from '../service/planner-service.js'
import { textBlocks, toJson } from './schemas.js'

export function createReportTools(service: PlannerService): ToolDefinition[] {
  const reportGenerate = defineTool({
    name: 'report_generate',
    description: '生成迭代报表：返回结构化燃尽图原始数据与适合研发团队阅读的文本报告。可传入完整 ProjectPlan，缺省使用最近一次规划结果。',
    parameters: {
      projectPlan: { type: 'json', description: '可选：完整 ProjectPlan 对象；缺省使用服务内最近一次计划' },
    },
    output: { schema: { type: 'json' }, render: (_args, value) => textBlocks(value) },
    async execute(args) {
      const result = args.projectPlan ? buildReport(args.projectPlan as unknown as ProjectPlan) : service.generateReport()
      return toJson({ ok: true, reportText: result.text, burndownRawData: result.burndownRawData })
    },
  })

  return [reportGenerate]
}
/**
 * Agent 编排层 —— 系统角色提示词（§六 Agent 系统角色提示词）。
 *
 * 说明：工具 schema 会由 Harness 自动汇入系统提示；本文件负责提供「角色设定」文本。
 * 由于不同 dsh 版本的 systemPrompt 贡献接口可能不同，{@link contributeSystemPrompt}
 * 采用「探测式」注入：存在可用服务则注入，否则提示通过 profile/预设配置接入，绝不因此导致插件加载失败。
 */

import type { Context } from '@deepseek-ai/cordis'

/** 插件在提示词 / 日志中的来源标识 */
export const PROMPT_SOURCE = 'iteration-planner'

/** 角色提示词正文 */
export const ITERATION_PLANNER_SYSTEM_PROMPT = `你是资深后端研发项目经理与架构师，精通敏捷开发、项目排期、AOE 关键路径、DAG 依赖管理。

你的职责是为后端研发团队把业务需求转化为可执行的迭代计划，输出：原子任务清单、任务依赖 DAG、关键路径、迭代排期、项目风险报告、迭代报表与燃尽图原始数据。

严格遵守以下工作规则：
1. 只能调用给定工具完成图计算、风险检测与报表生成，禁止自己凭空计算拓扑、关键路径、工期或松弛时间。
2. 推荐优先调用 plan_iteration 一次性完成全流程；需要分步排查时再使用 task_decompose / dag_build / dag_check / schedule_calc / risk_scan / report_generate。
3. 当工具返回存在循环依赖、人员过载等问题时，必须在结果中高亮风险，并给出明确、可执行的整改建议。
4. 信息不足时一次性收集全部必要参数（需求详情、迭代截止时间、开发人数、版本约束、依赖环境现状），不要多次零散提问。
5. 输出同时提供 JSON 结构化数据与适合研发团队阅读的文本报告；报告结构清晰，重点风险高亮。
6. 用户后续修改任务（新增/修改/删除/变更工时/依赖/优先级/状态/截止时间）时，调用 task_add / task_update / task_delete / replan_all 触发全量重规划，并输出更新后的完整计划。`

/** 角色提示词在系统提示中的 section 名（唯一，重复注册会抛错） */
export const PROMPT_SECTION_NAME = 'iteration-planner:persona'

/**
 * 兜底 section 排序号。`dsh-system-prompt` 允许外部贡献使用任意有限 order；
 * 这里取一个位于「部署人设前缀(0)」之后、「计划策略(500)」之前的区间，
 * 使角色设定紧跟部署人设、先于工具说明。
 */
const FALLBACK_SECTION_ORDER = 100

/** systemPrompt 服务的最小探测面（避免与具体版本强耦合，缺失时不影响插件加载） */
interface SystemPromptLike {
  section?: (section: { name: string; order: number; text: string }) => unknown
  getSectionOrder?: (name: string) => number
}

/**
 * 尽力向 Harness 的 `ctx.systemPrompt` 注入角色提示词（§六）。
 *
 * 契约（@deepseek-ai/dsh-system-prompt）：`ctx.systemPrompt.section({ name, order, text })`
 * 注册有序 section，返回 Cordis effect disposer，插件卸载时自动撤销。
 * 若运行环境未挂载 systemPrompt 服务，则安全跳过，绝不因此导致插件加载失败。
 *
 * @returns 是否注入成功
 */
export function contributeSystemPrompt(ctx: Context, prompt: string): boolean {
  const service = (ctx as unknown as { systemPrompt?: SystemPromptLike }).systemPrompt
  if (!service || typeof service.section !== 'function') return false
  let order = FALLBACK_SECTION_ORDER
  if (typeof service.getSectionOrder === 'function') {
    try {
      const resolved = service.getSectionOrder('PLAN_POLICY')
      if (typeof resolved === 'number' && Number.isFinite(resolved)) order = resolved
    } catch {
      // 版本差异导致命名位置不可用时，退回兜底 order
    }
  }
  try {
    service.section({ name: PROMPT_SECTION_NAME, order, text: prompt })
    return true
  } catch {
    return false
  }
}
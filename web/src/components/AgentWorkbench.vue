<script setup lang="ts">
/**
 * Agent 工作台：四项「进阶能力」的统一呈现面板（由 :component:`AgentDrawer` 的模式切换驱动）。
 *
 *  - ``collab``  多角色协作：一句话需求 → 需求分析 / 引擎排期 / 估算审阅 / 风险缓解 / 质量校验，
 *                步骤流**可见、可中断**（plan-act-observe），引擎排期由后端调用算法引擎完成；
 *  - ``whatif``  What-if 沙盘：自然语言情景（「后端延期 3 天」「再加 2 人」）→ 引擎重算 → 前后增量对比；
 *  - ``retro``   迭代复盘 + 学习：确定性统计 + 结论（可选写回长期记忆，反哺后续规划）；
 *  - ``patrol``  主动巡检：查看后台巡检 Agent 的发现清单，或立即执行一轮巡检。
 *
 * 边界：
 *  - 所有数值均来自后端（引擎返回值 / 确定性统计），前端只做展示换算，**不重算任何算法口径**；
 *  - 降级必须显式标注（``mode=rule`` + ``degradedReason``），不静默冒充模型结论；
 *  - 协作模式会真实创建计划（写操作），无 ``agent:write`` 权限时前端先行拦截并给出说明。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { ApiClientError, api } from '@/api/client'
import type {
  AgentChatResult,
  AgentCollabStep,
  AgentPendingAction,
  AgentStreamToolResult,
  PatrolFinding,
  PatrolFindingsResult,
  PatrolRunResult,
  RetroResult,
  WhatIfResult,
} from '@/api/types'

/** 工作台模式（与抽屉的模式切换一一对应） */
export type WorkbenchMode = 'collab' | 'whatif' | 'retro' | 'patrol'

const props = defineProps<{
  mode: WorkbenchMode
  /** 当前打开的计划 ID（What-if / 复盘必需） */
  planId?: string
  /** 当前计划需求摘要（展示用） */
  planLabel?: string
  /** 是否具备 agent:write 权限（协作会真实创建计划） */
  canWrite?: boolean
  /** 是否具备 memory:write 权限（复盘写回长期记忆） */
  canWriteMemory?: boolean
}>()

const emit = defineEmits<{ (event: 'open-plan', planId: string): void }>()

/** 工具名 → 中文说明（后端只回传工具名，这里仅用于人读展示） */
const TOOL_LABELS: Record<string, string> = {
  plan_iteration: '生成计划（拆解 → DAG → AOE → 风险 → 燃尽）',
  replan_all: '全量重规划',
  get_plan: '读取计划快照',
  list_plans: '列出计划',
  dag_build: '构建 DAG 视图',
  dag_check: 'Kahn 拓扑校验',
  schedule_calc: '关键路径 / 总工期',
  risk_scan: '风险扫描',
  report_generate: '生成迭代报表',
  task_add: '新增任务',
  task_update: '修改任务',
  task_delete: '删除任务',
  kb_search: '知识库检索',
  memory_search: '长期记忆召回',
}

function toolLabel(name: string): string {
  return TOOL_LABELS[name] ?? name
}

/** 错误提示统一格式（区分业务错误码与未知异常） */
function describeError(error: unknown): string {
  return error instanceof ApiClientError ? `${error.message}（${error.code}）` : String(error)
}

/** 后端 `at` 为 Unix 秒，统一换算为毫秒时间戳展示 */
function toMs(at?: number | null): number | null {
  if (at == null || !Number.isFinite(at)) return null
  return at > 1e11 ? at : at * 1000
}

function fmtTime(at?: number | null): string {
  const ms = toMs(at)
  if (ms == null) return '—'
  return new Date(ms).toLocaleString('zh-CN', { hour12: false })
}

/** 数值保留两位小数并去掉多余的 0 */
function fmtNum(value: unknown): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—'
  return Number.isInteger(value) ? String(value) : String(Number(value.toFixed(2)))
}

/** 带符号的增量（0 展示为 0，便于对齐比较） */
function fmtDelta(value: unknown): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—'
  const text = fmtNum(value)
  return value > 0 ? `+${text}` : text
}

function fmtPct(value: unknown): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—'
  return `${Number((value * 100).toFixed(1))}%`
}

/** 增量方向对应的语义色（"bad" 表示劣化） */
function deltaTone(value: unknown, lowerIsBetter = true): 'up-bad' | 'up-good' | 'flat' {
  if (typeof value !== 'number' || !Number.isFinite(value) || value === 0) return 'flat'
  const worse = lowerIsBetter ? value > 0 : value < 0
  return worse ? 'up-bad' : 'up-good'
}

onMounted(() => void loadSignalLegend())

/* ============================== 多角色协作 ============================== */

const collabRoles = ref<AgentCollabStep[]>([])
const collabRequirement = ref('')
const collabStart = ref('')
const collabDeadline = ref('')
const collabMembers = ref<number>()
const collabRunning = ref(false)
const collabSteps = ref<AgentCollabStep[]>([])
const collabTools = ref<AgentStreamToolResult[]>([])
const collabSuggestions = ref<AgentPendingAction[]>([])
const collabResult = ref<AgentChatResult>()
let collabAbort: AbortController | null = null

/** 角色时间线：按后端登记的角色顺序展示，未登记的角色追加在后（保证顺序稳定） */
const collabTimeline = computed(() => {
  const latest = new Map<string, AgentCollabStep>()
  for (const step of collabSteps.value) latest.set(step.role, step)
  const rows: { role: string; title: string; status: string; detail: string; at: number }[] = []
  const seen = new Set<string>()
  for (const role of collabRoles.value) {
    const step = latest.get(role.role)
    rows.push({
      role: role.role,
      title: step?.title || role.title || role.role,
      status: step?.status ?? 'pending',
      detail: step?.detail ?? '',
      at: step?.at ?? 0,
    })
    seen.add(role.role)
  }
  for (const [role, step] of latest) {
    if (seen.has(role)) continue
    rows.push({ role, title: step.title || role, status: step.status, detail: step.detail, at: step.at })
  }
  return rows
})

/** 步骤状态 → 展示口径（颜色 / 文案 / 图标） */
const STEP_STATES: Record<string, { tone: string; text: string }> = {
  pending: { tone: 'wait', text: '待执行' },
  running: { tone: 'run', text: '进行中' },
  done: { tone: 'done', text: '已完成' },
  failed: { tone: 'fail', text: '失败' },
  skipped: { tone: 'skip', text: '跳过' },
}

function stepState(status: string): { tone: string; text: string } {
  return STEP_STATES[status] ?? { tone: 'wait', text: status }
}

async function loadCollabRoles(): Promise<void> {
  try {
    collabRoles.value = await api.agentCollabSteps()
  } catch {
    // 图例加载失败不影响主流程（时间线会退化为后端实时回传的角色名）
    collabRoles.value = []
  }
}

async function startCollab(): Promise<void> {
  const requirement = collabRequirement.value.trim()
  if (!requirement || collabRunning.value) return
  if (props.canWrite === false) {
    ElMessage.warning('当前角色没有 agent:write 权限，无法执行协作规划')
    return
  }
  collabRunning.value = true
  collabSteps.value = []
  collabTools.value = []
  collabSuggestions.value = []
  collabResult.value = undefined
  if (collabRoles.value.length === 0) await loadCollabRoles()
  const controller = new AbortController()
  collabAbort = controller
  try {
    let final: AgentChatResult | undefined
    await api.agentCollabStream(
      {
        requirement,
        iterationStartDate: collabStart.value || undefined,
        iterationDeadline: collabDeadline.value || undefined,
        devMemberCount: collabMembers.value || undefined,
      },
      {
        onStep: (step) => {
          collabSteps.value = [...collabSteps.value, step]
        },
        onToolResult: (record) => {
          collabTools.value = [...collabTools.value, record]
        },
        onPending: (actions) => {
          if (actions.length > 0) collabSuggestions.value = actions
        },
        onFinal: (result) => {
          final = result
          collabResult.value = result
        },
      },
      controller.signal,
    )
    if (final?.planId) ElMessage.success('协作规划完成，计划已生成')
  } catch (error) {
    if (error instanceof ApiClientError && error.code === 'ABORTED') {
      ElMessage.info('已中断协作（已产出的步骤与结论保留）')
    } else {
      ElMessage.error(`协作规划失败：${describeError(error)}`)
    }
  } finally {
    collabRunning.value = false
    if (collabAbort === controller) collabAbort = null
  }
}

function stopCollab(): void {
  collabAbort?.abort()
}

/* ============================== What-if 沙盘 ============================== */

const whatifInstruction = ref('')
const whatifRunning = ref(false)
const whatifResult = ref<WhatIfResult>()

/** 快捷情景（覆盖最常见的三类扰动，点击即填入输入框） */
const WHATIF_PRESETS = ['后端延期 3 天', '再加 2 个人', '把关键任务工时缩短 2 天']

/** 前后对比表：数值行 + 交期行（交期单独展示日期与偏移，不做数值差） */
const whatifRows = computed(() => {
  const result = whatifResult.value
  if (!result) return []
  return [
    { label: '总工期（小时）', before: result.baseline.totalDurationHours, after: result.scenario.totalDurationHours, delta: result.delta.totalDurationHours, lowerIsBetter: true },
    { label: '预估工时（小时）', before: result.baseline.totalEstimatedHours, after: result.scenario.totalEstimatedHours, delta: result.delta.totalEstimatedHours, lowerIsBetter: true },
    { label: '任务数', before: result.baseline.taskCount, after: result.scenario.taskCount, delta: result.delta.taskCount, lowerIsBetter: true },
    { label: '关键路径任务数', before: result.baseline.criticalTaskCount, after: result.scenario.criticalTaskCount, delta: result.delta.criticalTaskCount, lowerIsBetter: true },
    { label: '风险总数', before: result.baseline.riskCount, after: result.scenario.riskCount, delta: result.delta.riskCount, lowerIsBetter: true },
    { label: '高风险数', before: result.baseline.highRiskCount, after: result.scenario.highRiskCount, delta: result.delta.highRiskCount, lowerIsBetter: true },
  ]
})

async function runWhatIf(): Promise<void> {
  const instruction = whatifInstruction.value.trim()
  if (!props.planId) {
    ElMessage.warning('请先在计划总览中打开一个迭代计划，再运行情景仿真')
    return
  }
  if (!instruction || whatifRunning.value) return
  whatifRunning.value = true
  whatifResult.value = undefined
  try {
    whatifResult.value = await api.agentWhatIf({ planId: props.planId, instruction })
  } catch (error) {
    ElMessage.error(`情景仿真失败：${describeError(error)}`)
  } finally {
    whatifRunning.value = false
  }
}

function usePreset(text: string): void {
  whatifInstruction.value = text
}

/* ============================== 迭代复盘 ============================== */

const retroActual = ref('')
const retroRisks = ref('')
const retroScope = ref('')
const retroWriteMemory = ref(true)
const retroRunning = ref(false)
const retroResult = ref<RetroResult>()

/** 复盘汇总卡片（全部取自后端确定性统计；缺失值展示为「—」） */
const retroCards = computed(() => {
  const metrics = (retroResult.value?.metrics ?? {}) as Record<string, any>
  const progress = (metrics.progress ?? {}) as Record<string, unknown>
  const delivery = (metrics.delivery ?? {}) as Record<string, unknown>
  const effort = (metrics.effort ?? {}) as Record<string, unknown>
  const risks = (metrics.risks ?? {}) as Record<string, unknown>
  const offsetDays = Number(delivery.offsetDays ?? NaN)
  return [
    { label: '任务完成度', value: fmtPct(progress.doneRatio), hint: `${fmtNum(progress.doneEstimatedHours)} / ${fmtNum(progress.totalEstimatedHours)} 小时` },
    { label: '时间进度', value: fmtPct(progress.elapsedRatio), hint: '已消耗的迭代时间占比' },
    { label: '进度偏差', value: typeof progress.driftDays === 'number' ? `${progress.driftDays} 天` : '—', hint: '正值表示落后于计划' },
    { label: '交付偏移', value: Number.isFinite(offsetDays) ? `${offsetDays} 天` : '—', hint: delivery.deadlineMet === true ? '计划完成不晚于截止' : '计划完成晚于截止' },
    { label: '风险兑现率', value: fmtPct(risks.materializedRate), hint: `已兑现 ${fmtNum(risks.materializedCount)} / 共 ${fmtNum(risks.total)} 项` },
    { label: '工时校准系数', value: fmtNum(effort.averageRatio), hint: '实际工时 ÷ 预估工时（1 表示估算准确）' },
  ]
})

/** 复盘明细：延期 / 阻塞 / 关键路径未指派责任人（引擎事实） */
const retroAlerts = computed(() => {
  const metrics = (retroResult.value?.metrics ?? {}) as Record<string, any>
  const progress = (metrics.progress ?? {}) as Record<string, any>
  const rows: { label: string; items: string[]; tone: 'danger' | 'warning' }[] = []
  const delayed = (progress.delayedTasks ?? []) as string[]
  const blocked = (progress.blockedTasks ?? []) as string[]
  const unassigned = (progress.unassignedCriticalTasks ?? []) as string[]
  if (delayed.length) rows.push({ label: '延期任务', items: delayed, tone: 'danger' })
  if (blocked.length) rows.push({ label: '阻塞任务', items: blocked, tone: 'danger' })
  if (unassigned.length) rows.push({ label: '关键路径未指派责任人', items: unassigned, tone: 'warning' })
  return rows
})

/** 工时偏差明细（实际 / 预估） */
const retroDeviations = computed(() => {
  const metrics = (retroResult.value?.metrics ?? {}) as Record<string, any>
  const effort = (metrics.effort ?? {}) as Record<string, any>
  return (effort.deviations ?? []) as {
    task: string
    estimateHours: number
    actualHours: number
    deltaHours: number
    ratio: number
  }[]
})

/** 工时偏差文本框 → 映射（每行 `任务ID 或 任务名 = 小时`，也支持「:」「：」分隔） */
function parseActualHours(text: string): Record<string, number> {
  const map: Record<string, number> = {}
  for (const raw of text.split('\n')) {
    const line = raw.trim()
    if (!line) continue
    const matched = line.match(/^(.+?)\s*[=:：]\s*([0-9]+(?:\.[0-9]+)?)$/)
    if (!matched) continue
    const value = Number(matched[2])
    if (!Number.isFinite(value)) continue
    map[matched[1].trim()] = value
  }
  return map
}

function parseList(text: string): string[] {
  return text
    .split(/[\n,，、]/)
    .map((item) => item.trim())
    .filter(Boolean)
}

async function runRetro(): Promise<void> {
  if (!props.planId) {
    ElMessage.warning('请先在计划总览中打开一个迭代计划，再执行复盘')
    return
  }
  if (retroRunning.value) return
  retroRunning.value = true
  retroResult.value = undefined
  try {
    retroResult.value = await api.agentRetro({
      planId: props.planId,
      actualHours: parseActualHours(retroActual.value),
      materializedRiskIds: parseList(retroRisks.value),
      scopeChanges: parseList(retroScope.value),
      writeMemory: retroWriteMemory.value && props.canWriteMemory !== false,
    })
  } catch (error) {
    ElMessage.error(`复盘失败：${describeError(error)}`)
  } finally {
    retroRunning.value = false
  }
}

/* ============================== 主动巡检 ============================== */

const patrolData = ref<PatrolFindingsResult>()
const patrolRunning = ref(false)
const patrolLastRun = ref<PatrolRunResult>()
const patrolOnlyCurrent = ref(false)
const signalLegend = ref<{ name: string; label: string }[]>([])

const patrolFindings = computed<PatrolFinding[]>(() => {
  const items = patrolData.value?.items ?? []
  if (!patrolOnlyCurrent.value || !props.planId) return items
  return items.filter((item) => item.planId === props.planId)
})

const severityTone: Record<string, 'danger' | 'warning' | 'info' | 'success'> = {
  critical: 'danger',
  warning: 'warning',
  info: 'info',
}

function findingTag(item: PatrolFinding): { tone: 'danger' | 'warning' | 'info' | 'success'; text: string } {
  if (item.status === 'resolved') return { tone: 'success', text: '已恢复' }
  const tone = severityTone[item.severity] ?? 'info'
  const prefix = item.status === 'new' ? '新发现' : item.escalated ? '已升级' : '持续'
  return { tone, text: prefix }
}

async function loadSignalLegend(): Promise<void> {
  try {
    const result = await api.patrolSignals()
    signalLegend.value = result.signals
  } catch {
    signalLegend.value = []
  }
}

async function loadPatrol(): Promise<void> {
  try {
    patrolData.value = await api.patrolFindings()
  } catch (error) {
    ElMessage.error(`巡检发现加载失败：${describeError(error)}`)
  }
}

async function runPatrol(): Promise<void> {
  if (patrolRunning.value) return
  patrolRunning.value = true
  try {
    patrolLastRun.value = await api.patrolRun()
    await loadPatrol()
    ElMessage.success('已完成一轮巡检')
  } catch (error) {
    ElMessage.error(`巡检执行失败：${describeError(error)}`)
  } finally {
    patrolRunning.value = false
  }
}

/* 切换模式时补齐各自所需的数据（避免一次性加载全部面板） */
watch(
  () => props.mode,
  (mode) => {
    if (mode === 'collab' && collabRoles.value.length === 0) void loadCollabRoles()
    if (mode === 'patrol') void loadPatrol()
  },
  { immediate: true },
)
</script>

<template>
  <!-- ============================ 多角色协作 ============================ -->
  <div v-if="mode === 'collab'" class="wb">
    <p class="wb-hint">
      一句话需求 → 多角色协作跑完整链路（<b>需求分析 → 引擎排期 → 估算审阅 → 风险缓解 → 质量校验</b>）。
      引擎排期会<b>真实创建计划</b>，估算审阅只给出建议、不自动落库。
    </p>

    <div class="wb-legend">
      <span v-for="role in collabRoles" :key="role.role" class="wb-chip">{{ role.title || role.role }}</span>
    </div>

    <el-input
      v-model="collabRequirement"
      type="textarea"
      :rows="3"
      maxlength="2000"
      show-word-limit
      placeholder="例如：为电商结算模块做一个两周迭代，包含优惠券、对账与退款"
      :disabled="collabRunning"
    />

    <div class="wb-grid3">
      <el-input v-model="collabStart" placeholder="起始日期 YYYY-MM-DD" :disabled="collabRunning" />
      <el-input v-model="collabDeadline" placeholder="截止日期 YYYY-MM-DD" :disabled="collabRunning" />
      <el-input-number
        v-model="collabMembers"
        :min="1"
        :max="200"
        placeholder="开发人数"
        controls-position="right"
        :disabled="collabRunning"
        class="wb-span"
      />
    </div>

    <div class="wb-actions">
      <el-button type="primary" :loading="collabRunning" :disabled="!collabRequirement.trim()" @click="startCollab">
        开始协作
      </el-button>
      <el-button v-if="collabRunning" @click="stopCollab">中断</el-button>
      <span v-if="canWrite === false" class="wb-note">当前角色无 agent:write 权限</span>
    </div>

    <div v-if="collabTimeline.length" class="wb-block">
      <div class="wb-block-title">角色步骤（可中断，中间过程全程可见）</div>
      <div v-for="row in collabTimeline" :key="row.role" class="wb-step" :class="`wb-step--${stepState(row.status).tone}`">
        <div class="wb-step-head">
          <span class="wb-step-title">{{ row.title }}</span>
          <el-tag size="small" :type="stepState(row.status).tone === 'fail' ? 'danger' : stepState(row.status).tone === 'done' ? 'success' : stepState(row.status).tone === 'run' ? 'primary' : 'info'" effect="plain">
            {{ stepState(row.status).text }}
          </el-tag>
        </div>
        <div v-if="row.detail" class="wb-step-detail">{{ row.detail }}</div>
      </div>
    </div>

    <div v-if="collabTools.length" class="wb-block">
      <div class="wb-block-title">引擎调用（算法结果唯一来源）</div>
      <div v-for="(record, index) in collabTools" :key="`${record.name}-${index}`" class="wb-tool">
        <span class="wb-tool-name">{{ toolLabel(record.name) }}</span>
        <el-tag size="small" :type="record.ok ? 'success' : 'danger'" effect="plain">
          {{ record.ok ? '成功' : '失败' }}
        </el-tag>
        <span v-if="record.error" class="wb-tool-error">{{ record.error }}</span>
      </div>
    </div>

    <el-alert
      v-if="collabSuggestions.length"
      type="warning"
      :closable="false"
      show-icon
      title="估算审阅建议（需确认后执行，本轮未落库）"
      class="wb-alert"
    >
      <ul class="wb-list">
        <li v-for="action in collabSuggestions" :key="action.actionId">{{ action.label }}</li>
      </ul>
    </el-alert>

    <div v-if="collabResult" class="wb-block">
      <div class="wb-block-title">
        协作结论
        <el-tag v-if="collabResult.planId" size="small" type="success" effect="plain">已生成计划</el-tag>
      </div>
      <pre class="wb-report">{{ collabResult.content || '（无结论文本）' }}</pre>
      <el-button v-if="collabResult.planId" size="small" @click="emit('open-plan', collabResult.planId)">
        查看该计划
      </el-button>
    </div>
  </div>

  <!-- ============================ What-if 沙盘 ============================ -->
  <div v-else-if="mode === 'whatif'" class="wb">
    <p class="wb-hint">
      用自然语言描述情景，后端**临时**生成一份对照计划并调用引擎重算关键路径与交付日期，
      给出 before / after 增量后立即回收，<b>不影响基线计划</b>。
    </p>
    <div class="wb-target">
      <span class="wb-note">基线计划：</span>
      <span class="wb-target-name">{{ planLabel || planId || '未打开计划' }}</span>
    </div>

    <div class="wb-presets">
      <el-tag
        v-for="preset in WHATIF_PRESETS"
        :key="preset"
        class="wb-preset"
        effect="plain"
        :class="{ 'is-disabled': whatifRunning }"
        @click="usePreset(preset)"
      >
        {{ preset }}
      </el-tag>
    </div>

    <el-input
      v-model="whatifInstruction"
      type="textarea"
      :rows="2"
      maxlength="500"
      show-word-limit
      placeholder="例如：后端延期 3 天，并再加 2 个人"
      :disabled="whatifRunning"
    />
    <div class="wb-actions">
      <el-button type="primary" :loading="whatifRunning" :disabled="!whatifInstruction.trim() || !planId" @click="runWhatIf">
        运行仿真
      </el-button>
      <span v-if="!planId" class="wb-note">需先打开一个迭代计划</span>
    </div>

    <template v-if="whatifResult">
      <el-alert
        v-if="whatifResult.degradedReason"
        type="warning"
        :closable="false"
        show-icon
        :title="whatifResult.degradedReason"
        class="wb-alert"
      />
      <div class="wb-intent">
        <el-tag size="small" :type="whatifResult.mode === 'llm' ? 'primary' : 'info'" effect="plain">
          {{ whatifResult.mode === 'llm' ? `模型解析：${whatifResult.model ?? '已配置'}` : '规则解析' }}
        </el-tag>
        <span class="wb-intent-text">{{ whatifResult.intent }}</span>
      </div>

      <el-alert
        v-if="whatifResult.unparsed"
        type="info"
        :closable="false"
        show-icon
        title="未识别到可执行的情景改动，请换一种说法（如「后端延期 3 天」「再加 2 个人」）"
        class="wb-alert"
      />

      <div class="wb-block">
        <div class="wb-block-title">前后对比</div>
        <table class="wb-table">
          <thead>
            <tr><th>指标</th><th class="wb-num">基线</th><th class="wb-num">情景</th><th class="wb-num">增量</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in whatifRows" :key="row.label">
              <td>{{ row.label }}</td>
              <td class="wb-num">{{ fmtNum(row.before) }}</td>
              <td class="wb-num">{{ fmtNum(row.after) }}</td>
              <td class="wb-num" :class="deltaTone(row.delta, row.lowerIsBetter)">{{ fmtDelta(row.delta) }}</td>
            </tr>
            <tr>
              <td>计划完成日期</td>
              <td class="wb-num">{{ whatifResult.baseline.plannedEndDate || '—' }}</td>
              <td class="wb-num">{{ whatifResult.scenario.plannedEndDate || '—' }}</td>
              <td class="wb-num" :class="deltaTone(whatifResult.deadlineShiftDays)">
                {{ typeof whatifResult.deadlineShiftDays === 'number' ? `${fmtDelta(whatifResult.deadlineShiftDays)} 天` : '—' }}
              </td>
            </tr>
            <tr>
              <td>交期满足</td>
              <td class="wb-num">{{ whatifResult.baseline.deadlineMet === null ? '—' : whatifResult.baseline.deadlineMet ? '满足' : '超出' }}</td>
              <td class="wb-num">{{ whatifResult.scenario.deadlineMet === null ? '—' : whatifResult.scenario.deadlineMet ? '满足' : '超出' }}</td>
              <td class="wb-num">—</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="wb-block">
        <div class="wb-block-title">关键路径变化</div>
        <div class="wb-diff">
          <div>
            <span class="wb-note">新增进入关键路径</span>
            <span v-if="!whatifResult.criticalPathAdded.length" class="wb-empty">无</span>
            <el-tag v-for="name in whatifResult.criticalPathAdded" :key="`add-${name}`" size="small" type="warning" effect="plain" class="wb-tag">{{ name }}</el-tag>
          </div>
          <div>
            <span class="wb-note">脱离关键路径</span>
            <span v-if="!whatifResult.criticalPathRemoved.length" class="wb-empty">无</span>
            <el-tag v-for="name in whatifResult.criticalPathRemoved" :key="`del-${name}`" size="small" type="success" effect="plain" class="wb-tag">{{ name }}</el-tag>
          </div>
          <div>
            <span class="wb-note">新增风险</span>
            <span v-if="!whatifResult.newRisks.length" class="wb-empty">无</span>
            <div v-for="risk in whatifResult.newRisks" :key="`new-${risk}`" class="wb-risk">{{ risk }}</div>
          </div>
          <div>
            <span class="wb-note">解除风险</span>
            <span v-if="!whatifResult.resolvedRisks.length" class="wb-empty">无</span>
            <div v-for="risk in whatifResult.resolvedRisks" :key="`old-${risk}`" class="wb-risk">{{ risk }}</div>
          </div>
        </div>
      </div>

      <div v-if="whatifResult.edits.length" class="wb-block">
        <div class="wb-block-title">落地改动</div>
        <table class="wb-table">
          <thead>
            <tr><th>任务</th><th>字段</th><th>值</th><th>结果</th></tr>
          </thead>
          <tbody>
            <tr v-for="(edit, index) in whatifResult.edits" :key="`${edit.task}-${index}`">
              <td>{{ edit.task }}</td>
              <td>{{ edit.field }}</td>
              <td>{{ String(edit.value ?? '—') }}</td>
              <td>
                <el-tag size="small" :type="edit.applied ? 'success' : 'danger'" effect="plain">
                  {{ edit.applied ? '已应用' : '未应用' }}
                </el-tag>
                <span v-if="edit.error" class="wb-tool-error">{{ edit.error }}</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <el-alert type="info" :closable="false" show-icon :title="whatifResult.summary" class="wb-alert" />
      <ul v-if="whatifResult.notes.length" class="wb-list">
        <li v-for="note in whatifResult.notes" :key="note">{{ note }}</li>
      </ul>
      <p v-if="whatifResult.ephemeral" class="wb-note">对照计划已即时回收，未写入持久化存储。</p>
    </template>
  </div>

  <!-- ============================ 迭代复盘 ============================ -->
  <div v-else-if="mode === 'retro'" class="wb">
    <p class="wb-hint">
      用引擎事实 + 你提供的实际数据算偏差：工时偏差 / 风险兑现率 / 交付偏移 / 估算校准。
      结论可写回长期记忆，用于后续规划的估算校准。
    </p>
    <div class="wb-target">
      <span class="wb-note">复盘计划：</span>
      <span class="wb-target-name">{{ planLabel || planId || '未打开计划' }}</span>
    </div>

    <div class="wb-field">
      <label>实际工时（可选，每行一项：<code>任务ID 或 任务名 = 小时</code>）</label>
      <el-input v-model="retroActual" type="textarea" :rows="3" placeholder="登录接口 = 12&#10;T-3 = 6.5" :disabled="retroRunning" />
    </div>
    <div class="wb-field">
      <label>已兑现风险 ID（可选，逗号分隔；用于计算风险兑现率）</label>
      <el-input v-model="retroRisks" placeholder="R-001, R-003" :disabled="retroRunning" />
    </div>
    <div class="wb-field">
      <label>范围变更说明（可选，每行一条）</label>
      <el-input v-model="retroScope" type="textarea" :rows="2" placeholder="新增对账需求&#10;砍掉导出功能" :disabled="retroRunning" />
    </div>
    <div class="wb-actions">
      <el-button type="primary" :loading="retroRunning" :disabled="!planId" @click="runRetro">生成复盘</el-button>
      <el-switch v-model="retroWriteMemory" active-text="结论写回长期记忆" :disabled="retroRunning || canWriteMemory === false" />
      <span v-if="!planId" class="wb-note">需先打开一个迭代计划</span>
    </div>

    <template v-if="retroResult">
      <el-alert
        v-if="retroResult.degradedReason"
        type="warning"
        :closable="false"
        show-icon
        :title="retroResult.degradedReason"
        class="wb-alert"
      />
      <div class="wb-cards">
        <div v-for="card in retroCards" :key="card.label" class="wb-card">
          <div class="wb-card-label">{{ card.label }}</div>
          <div class="wb-card-value">{{ card.value }}</div>
          <div class="wb-card-hint">{{ card.hint }}</div>
        </div>
      </div>

      <div v-if="retroAlerts.length" class="wb-block">
        <div class="wb-block-title">需要关注（引擎事实）</div>
        <div v-for="row in retroAlerts" :key="row.label" class="wb-alert-row">
          <span class="wb-note">{{ row.label }}</span>
          <el-tag v-for="item in row.items" :key="item" size="small" :type="row.tone" effect="plain" class="wb-tag">{{ item }}</el-tag>
        </div>
      </div>

      <div v-if="retroDeviations.length" class="wb-block">
        <div class="wb-block-title">工时偏差明细</div>
        <table class="wb-table">
          <thead>
            <tr><th>任务</th><th class="wb-num">预估</th><th class="wb-num">实际</th><th class="wb-num">偏差</th><th class="wb-num">系数</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in retroDeviations" :key="row.task">
              <td>{{ row.task }}</td>
              <td class="wb-num">{{ fmtNum(row.estimateHours) }}</td>
              <td class="wb-num">{{ fmtNum(row.actualHours) }}</td>
              <td class="wb-num" :class="deltaTone(row.deltaHours)">{{ fmtDelta(row.deltaHours) }}</td>
              <td class="wb-num">{{ fmtNum(row.ratio) }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <el-alert v-else type="info" :closable="false" show-icon class="wb-alert"
        title="未提供实际工时，无法计算工时偏差与估算校准曲线（引擎计划中不含实际工时字段）" />

      <div v-if="retroResult.summary" class="wb-block">
        <div class="wb-block-title">复盘结论</div>
        <p class="wb-summary">{{ retroResult.summary }}</p>
        <div v-if="retroResult.highlights.length" class="wb-list-block">
          <div class="wb-note">做得好的</div>
          <ul class="wb-list"><li v-for="item in retroResult.highlights" :key="item">{{ item }}</li></ul>
        </div>
        <div v-if="retroResult.problems.length" class="wb-list-block">
          <div class="wb-note">问题</div>
          <ul class="wb-list"><li v-for="item in retroResult.problems" :key="item">{{ item }}</li></ul>
        </div>
        <div v-if="retroResult.actions.length" class="wb-list-block">
          <div class="wb-note">改进动作</div>
          <ul class="wb-list"><li v-for="item in retroResult.actions" :key="item">{{ item }}</li></ul>
        </div>
      </div>

      <div v-if="retroResult.memories.length" class="wb-block">
        <div class="wb-block-title">
          学习沉淀
          <el-tag size="small" :type="retroResult.memoryWritten > 0 ? 'success' : 'info'" effect="plain">
            {{ retroResult.memoryEnabled ? `已写回 ${retroResult.memoryWritten} 条` : '长期记忆未启用' }}
          </el-tag>
        </div>
        <ul class="wb-list"><li v-for="item in retroResult.memories" :key="item">{{ item }}</li></ul>
      </div>
    </template>
  </div>

  <!-- ============================ 主动巡检 ============================ -->
  <div v-else class="wb">
    <p class="wb-hint">
      后台巡检 Agent 定时比对引擎事实（<b>进度偏差 / 关键路径漂移 / 人员过载 / 依赖阻塞 / 交付风险</b>），
      仅推送<b>增量</b>预警（同一问题处于冷却期内不重复打扰），异常经 WebSocket 实时送达通知中心。
    </p>

    <div class="wb-patrol-status">
      <el-tag size="small" :type="patrolData?.enabled ? 'success' : 'info'" effect="plain">
        {{ patrolData?.enabled ? '巡检已启用' : '巡检未启用' }}
      </el-tag>
      <el-tag size="small" :type="patrolData?.running ? 'primary' : 'info'" effect="plain">
        {{ patrolData?.running ? '运行中' : '空闲' }}
      </el-tag>
      <span class="wb-note">周期 {{ patrolData?.intervalSeconds ?? '—' }} 秒</span>
      <span class="wb-note">上次运行 {{ fmtTime(patrolData?.lastRunAt) }}</span>
    </div>

    <div class="wb-actions">
      <el-button type="primary" size="small" :loading="patrolRunning" @click="runPatrol">立即巡检</el-button>
      <el-button size="small" @click="loadPatrol">刷新发现</el-button>
      <el-checkbox v-model="patrolOnlyCurrent" :disabled="!planId" label="仅当前计划" />
    </div>

    <el-alert
      v-if="patrolLastRun"
      type="info"
      :closable="false"
      show-icon
      class="wb-alert"
      :title="`本轮巡检 ${patrolLastRun.plans} 个计划 / ${patrolLastRun.signals} 个信号，新增或升级预警 ${patrolLastRun.alerts} 条，恢复 ${patrolLastRun.resolved} 条，耗时 ${patrolLastRun.durationMs} ms`"
    >
      <ul v-if="patrolLastRun.errors.length" class="wb-list">
        <li v-for="error in patrolLastRun.errors" :key="error">部分计划巡检失败：{{ error }}</li>
      </ul>
    </el-alert>

    <div v-if="signalLegend.length" class="wb-legend">
      <span v-for="signal in signalLegend" :key="signal.name" class="wb-chip">{{ signal.label }}</span>
    </div>

    <div v-if="patrolFindings.length" class="wb-block">
      <div v-for="item in patrolFindings" :key="item.findingId" class="wb-finding">
        <div class="wb-finding-head">
          <el-tag size="small" :type="findingTag(item).tone" effect="plain">{{ findingTag(item).text }}</el-tag>
          <span class="wb-finding-title">{{ item.title }}</span>
          <span class="wb-finding-signal">{{ item.signalLabel || item.signal }}</span>
        </div>
        <div v-if="item.detail" class="wb-finding-detail">{{ item.detail }}</div>
        <div class="wb-finding-foot">
          <span class="wb-note">{{ item.planRequirement || item.planId }}</span>
          <span class="wb-note">{{ fmtTime(item.at) }}</span>
        </div>
      </div>
    </div>

    <el-empty v-else description="暂无巡检发现（计划健康或巡检尚未运行）" :image-size="60" />
  </div>
</template>

<style scoped>
.wb {
  display: flex;
  flex-direction: column;
  gap: 12px;
  color: var(--ip-text);
}

.wb-hint {
  margin: 0;
  font-size: 12px;
  line-height: 1.7;
  color: var(--ip-text-secondary);
}

.wb-note {
  font-size: 12px;
  color: var(--ip-text-muted);
}

.wb-empty {
  font-size: 12px;
  color: var(--ip-text-muted);
  margin-left: 6px;
}

.wb-block {
  border: 1px solid var(--ip-border);
  border-radius: var(--ip-radius);
  padding: 10px 12px;
  background: var(--ip-surface);
}

.wb-block-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  font-weight: 600;
  color: var(--ip-text);
  margin-bottom: 8px;
}

.wb-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.wb-chip {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid var(--ip-border);
  background: var(--ip-surface-sunk);
  color: var(--ip-text-secondary);
}

.wb-grid3 {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
}

.wb-span {
  width: 100%;
}

.wb-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.wb-alert {
  margin: 0;
}

.wb-list {
  margin: 4px 0 0;
  padding-left: 18px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--ip-text-secondary);
}

.wb-list-block {
  margin-top: 8px;
}

.wb-step {
  border-left: 3px solid var(--ip-border-strong);
  padding: 6px 0 6px 10px;
  margin-bottom: 6px;
}

.wb-step--run {
  border-left-color: var(--ip-brand);
}

.wb-step--done {
  border-left-color: var(--ip-success);
}

.wb-step--fail {
  border-left-color: var(--ip-danger);
}

.wb-step--skip {
  border-left-color: var(--ip-text-muted);
}

.wb-step-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.wb-step-title {
  font-size: 13px;
  font-weight: 600;
}

.wb-step-detail {
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--ip-text-secondary);
  white-space: pre-wrap;
}

.wb-tool {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  padding: 3px 0;
}

.wb-tool-name {
  color: var(--ip-text-secondary);
}

.wb-tool-error {
  font-size: 12px;
  color: var(--ip-danger);
}

.wb-report {
  margin: 0 0 8px;
  font-size: 12px;
  line-height: 1.8;
  font-family: var(--ip-font-sans);
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--ip-text-secondary);
}

.wb-target {
  display: flex;
  align-items: center;
  gap: 6px;
}

.wb-target-name {
  font-size: 13px;
  font-weight: 600;
}

.wb-presets {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.wb-preset {
  cursor: pointer;
}

.wb-intent {
  display: flex;
  align-items: center;
  gap: 8px;
}

.wb-intent-text {
  font-size: 12px;
  color: var(--ip-text-secondary);
}

.wb-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}

.wb-table th,
.wb-table td {
  padding: 5px 6px;
  border-bottom: 1px solid var(--ip-border);
  text-align: left;
}

.wb-table th {
  font-weight: 600;
  color: var(--ip-text-secondary);
  background: var(--ip-surface-sunk);
}

.wb-num {
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.up-bad {
  color: var(--ip-danger);
  font-weight: 600;
}

.up-good {
  color: var(--ip-success);
  font-weight: 600;
}

.flat {
  color: var(--ip-text-muted);
}

.wb-diff {
  display: flex;
  flex-direction: column;
  gap: 8px;
  font-size: 12px;
}

.wb-tag {
  margin: 0 4px 4px 0;
}

.wb-risk {
  font-size: 12px;
  line-height: 1.7;
  color: var(--ip-text-secondary);
}

.wb-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.wb-field label {
  font-size: 12px;
  color: var(--ip-text-secondary);
}

.wb-field code {
  font-family: var(--ip-font-mono);
  font-size: 11px;
  padding: 0 4px;
  border-radius: 3px;
  background: var(--ip-surface-sunk);
}

.wb-cards {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
}

.wb-card {
  border: 1px solid var(--ip-border);
  border-radius: var(--ip-radius);
  padding: 8px 10px;
  background: var(--ip-surface-sunk);
}

.wb-card-label {
  font-size: 11px;
  color: var(--ip-text-muted);
}

.wb-card-value {
  margin-top: 2px;
  font-size: 18px;
  font-weight: 600;
  font-family: var(--ip-font-display);
  color: var(--ip-text);
}

.wb-card-hint {
  margin-top: 2px;
  font-size: 11px;
  line-height: 1.6;
  color: var(--ip-text-muted);
}

.wb-alert-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 4px;
}

.wb-summary {
  margin: 0 0 6px;
  font-size: 13px;
  line-height: 1.8;
  color: var(--ip-text);
}

.wb-patrol-status {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.wb-finding {
  border: 1px solid var(--ip-border);
  border-radius: var(--ip-radius);
  padding: 8px 10px;
  margin-bottom: 8px;
  background: var(--ip-surface);
}

.wb-finding-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.wb-finding-title {
  font-size: 13px;
  font-weight: 600;
}

.wb-finding-signal {
  font-size: 11px;
  color: var(--ip-text-muted);
}

.wb-finding-detail {
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--ip-text-secondary);
}

.wb-finding-foot {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  margin-top: 4px;
}
</style>

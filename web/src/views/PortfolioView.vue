<script setup lang="ts">
/**
 * 项目组合对比：把全部迭代计划摘要并排呈现，做跨计划的产能 / 工期 / 风险对照。
 *
 * ⚠️ 定位说明：
 *  - 数据源为引擎 `GET /plans` 返回的计划摘要（`PlanSummary`），所有指标均为**展示层
 *    聚合**（求和、比值），不参与任何业务判定；
 *  - 「可用工时」按迭代窗口内的工作日 × 单开发人单日 8h × 开发人数推算，仅用于展示
 *    团队负荷率；它不替代引擎的关键路径工期，也不改写任何排期结果；
 *  - 高风险 / 风险总数直接取引擎返回值。
 */
import { computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import type { EChartsOption } from 'echarts'
import type { PlanSummary } from '@/api/types'
import EChart from '@/components/EChart.vue'
import { usePlanStore } from '@/stores/plan'
import { hoursToWorkDays, truncate } from '@/utils/format'
import { CHART_AXIS_LINE, CHART_SPLIT_LINE, CHART_TEXT, TOKEN } from '@/utils/palette'
import { WORK_HOURS_PER_DAY, enumerateWorkdays, formatISODate } from '@/utils/workday'

const store = usePlanStore()
const router = useRouter()

const summaries = computed<PlanSummary[]>(() => store.summaries)

/** 今天的 ISO 日期（用于逾期判定，纯展示） */
const todayISO = formatISODate(new Date())

/** 组合行模型 */
interface PortfolioRow {
  summary: PlanSummary
  /** 迭代窗口内的工作日数 */
  availableDays: number
  /** 可用总工时 = 工作日 × 8h × 开发人数 */
  availableHours: number
  /** 团队负荷率 = 预估总工时 / 可用总工时 */
  loadRatio: number
  /** 关键工期占用率 = 关键路径工期 / (工作日 × 8h) */
  durationRatio: number
  /** 距截止的剩余工作日（已逾期为 0） */
  remainingDays: number
  /** 是否已过迭代截止日期 */
  overdue: boolean
  /** 人均预估工时 */
  perCapitaHours: number
}

/** 逐计划聚合（全部取自引擎摘要字段） */
const rows = computed<PortfolioRow[]>(() =>
  summaries.value.map((summary) => {
    const availableDays = Math.max(
      1,
      enumerateWorkdays(summary.iterationStartDate, summary.iterationDeadline).length,
    )
    const availableHours = availableDays * WORK_HOURS_PER_DAY * Math.max(1, summary.devMemberCount)
    const overdue = summary.iterationDeadline < todayISO
    const remainingDays = overdue
      ? 0
      : enumerateWorkdays(todayISO, summary.iterationDeadline).length
    return {
      summary,
      availableDays,
      availableHours,
      loadRatio: availableHours > 0 ? summary.totalEstimatedHours / availableHours : 0,
      durationRatio: summary.totalDurationHours / (availableDays * WORK_HOURS_PER_DAY),
      remainingDays,
      overdue,
      perCapitaHours: summary.totalEstimatedHours / Math.max(1, summary.devMemberCount),
    }
  }),
)

/** 组合汇总 KPI */
const totals = computed(() => {
  const list = summaries.value
  return {
    plans: list.length,
    tasks: list.reduce((sum, item) => sum + item.taskCount, 0),
    hours: list.reduce((sum, item) => sum + item.totalEstimatedHours, 0),
    highRisk: list.reduce((sum, item) => sum + item.highRiskCount, 0),
    risk: list.reduce((sum, item) => sum + item.riskCount, 0),
    overdue: rows.value.filter((row) => row.overdue).length,
  }
})

/** 图表横轴：计划短标签（同一需求文本截断） */
const axisLabels = computed(() => rows.value.map((row) => truncate(row.summary.rawRequirement, 12)))

/** 工期 / 工时对比（分组柱） */
const workloadOption = computed<EChartsOption>(() => ({
  tooltip: {
    trigger: 'axis',
    axisPointer: { type: 'shadow' },
    valueFormatter: (value: unknown) => `${String(value)} h`,
  },
  legend: { bottom: 0, itemWidth: 12, itemHeight: 8, textStyle: CHART_TEXT, data: ['预估总工时', '关键路径工期'] },
  grid: { left: 56, right: 22, top: 18, bottom: 58 },
  xAxis: {
    type: 'category',
    data: axisLabels.value,
    axisLabel: { ...CHART_TEXT, interval: 0, rotate: axisLabels.value.length > 4 ? 20 : 0 },
    axisLine: CHART_AXIS_LINE,
  },
  yAxis: { type: 'value', name: '工时(h)', nameTextStyle: CHART_TEXT, splitLine: CHART_SPLIT_LINE, axisLabel: CHART_TEXT },
  series: [
    {
      name: '预估总工时',
      type: 'bar',
      barWidth: 18,
      data: rows.value.map((row) => row.summary.totalEstimatedHours),
      itemStyle: { color: TOKEN.accent, borderRadius: [2, 2, 0, 0] },
    },
    {
      name: '关键路径工期',
      type: 'bar',
      barWidth: 18,
      data: rows.value.map((row) => row.summary.totalDurationHours),
      itemStyle: { color: TOKEN.brand, borderRadius: [2, 2, 0, 0] },
    },
  ],
}))

/** 团队负荷率（柱，>100% 红色告警；纯展示比值） */
const loadOption = computed<EChartsOption>(() => ({
  tooltip: {
    trigger: 'axis',
    axisPointer: { type: 'shadow' },
    valueFormatter: (value: unknown) => `${String(value)}%`,
  },
  grid: { left: 56, right: 22, top: 18, bottom: 58 },
  xAxis: {
    type: 'category',
    data: axisLabels.value,
    axisLabel: { ...CHART_TEXT, interval: 0, rotate: axisLabels.value.length > 4 ? 20 : 0 },
    axisLine: CHART_AXIS_LINE,
  },
  yAxis: {
    type: 'value',
    name: '负荷率(%)',
    nameTextStyle: CHART_TEXT,
    splitLine: CHART_SPLIT_LINE,
    axisLabel: CHART_TEXT,
  },
  series: [
    {
      type: 'bar',
      barWidth: 26,
      data: rows.value.map((row) => ({
        value: Number((row.loadRatio * 100).toFixed(1)),
        itemStyle: {
          color: row.loadRatio > 1 ? TOKEN.danger : row.loadRatio > 0.85 ? TOKEN.signal : TOKEN.success,
          borderRadius: [2, 2, 0, 0],
        },
      })),
      label: { show: true, position: 'top', formatter: '{c}%', fontSize: 11, color: TOKEN.slate },
    },
  ],
}))

/** 风险构成（堆叠柱：高风险 / 其他风险） */
const riskOption = computed<EChartsOption>(() => ({
  tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, valueFormatter: (value: unknown) => `${String(value)} 条` },
  legend: { bottom: 0, itemWidth: 12, itemHeight: 8, textStyle: CHART_TEXT, data: ['高风险', '其他风险'] },
  grid: { left: 48, right: 22, top: 18, bottom: 58 },
  xAxis: {
    type: 'category',
    data: axisLabels.value,
    axisLabel: { ...CHART_TEXT, interval: 0, rotate: axisLabels.value.length > 4 ? 20 : 0 },
    axisLine: CHART_AXIS_LINE,
  },
  yAxis: { type: 'value', minInterval: 1, name: '风险数', nameTextStyle: CHART_TEXT, splitLine: CHART_SPLIT_LINE, axisLabel: CHART_TEXT },
  series: [
    {
      name: '高风险',
      type: 'bar',
      stack: 'risk',
      barWidth: 22,
      data: rows.value.map((row) => row.summary.highRiskCount),
      itemStyle: { color: TOKEN.danger },
    },
    {
      name: '其他风险',
      type: 'bar',
      stack: 'risk',
      barWidth: 22,
      data: rows.value.map((row) => Math.max(0, row.summary.riskCount - row.summary.highRiskCount)),
      itemStyle: { color: TOKEN.signal },
    },
  ],
}))

function open(planId: string): void {
  void router.push({ name: 'plan-overview', params: { planId } })
}

onMounted(() => {
  void store.loadPlans()
})
</script>

<template>
  <div class="ip-page">
    <div class="ip-page__actions">
      <el-button type="primary" :icon="'MagicStick'" @click="router.push({ name: 'plan-create' })">
        新建迭代计划
      </el-button>
      <el-button :icon="'Refresh'" @click="store.loadPlans()">刷新</el-button>
      <span class="ip-muted">按团队负荷率从高到低排序，便于优先关注压力最大的迭代</span>
    </div>

    <el-empty v-if="store.summaries.length === 0" description="暂无可对比的迭代计划" />

    <template v-else>
      <div class="ip-panel">
        <div class="ip-panel__title"><span>组合概览</span></div>
        <div class="ip-metric-grid">
          <div class="ip-metric">
            <span class="ip-metric__label">计划总数</span>
            <strong class="ip-metric__value">{{ totals.plans }}</strong>
            <span class="ip-muted">共 {{ totals.tasks }} 项任务</span>
          </div>
          <div class="ip-metric">
            <span class="ip-metric__label">预估总工时</span>
            <strong class="ip-metric__value">{{ totals.hours }}h</strong>
            <span class="ip-muted">{{ hoursToWorkDays(totals.hours) }}（单人折算）</span>
          </div>
          <div class="ip-metric">
            <span class="ip-metric__label">风险总数</span>
            <strong class="ip-metric__value" :class="{ 'is-danger': totals.highRisk > 0 }">{{ totals.risk }}</strong>
            <span class="ip-muted">其中高风险 {{ totals.highRisk }} 条</span>
          </div>
          <div class="ip-metric">
            <span class="ip-metric__label">已过截止</span>
            <strong class="ip-metric__value" :class="{ 'is-danger': totals.overdue > 0 }">{{ totals.overdue }}</strong>
            <span class="ip-muted">个计划已超过迭代截止日期</span>
          </div>
        </div>
      </div>

      <div class="ip-grid ip-grid--2">
        <div class="ip-panel">
          <div class="ip-panel__title"><span>工期与工时对比</span></div>
          <EChart :option="workloadOption" height="300px" />
        </div>
        <div class="ip-panel">
          <div class="ip-panel__title">
            <span>团队负荷率</span>
            <span class="ip-muted" style="font-size: 12px">预估总工时 / 可用总工时</span>
          </div>
          <EChart :option="loadOption" height="300px" />
        </div>
      </div>

      <div class="ip-panel">
        <div class="ip-panel__title"><span>风险构成</span></div>
        <EChart :option="riskOption" height="300px" />
      </div>

      <div class="ip-panel">
        <div class="ip-panel__title">
          <span>计划对照表</span>
          <el-tag size="small" effect="plain">数据源：引擎 GET /plans 摘要</el-tag>
        </div>
        <el-table
          v-loading="store.listLoading"
          :data="rows"
          stripe
          :default-sort="{ prop: 'loadRatio', order: 'descending' }"
          empty-text="暂无计划"
        >
          <el-table-column label="原始需求" min-width="220">
            <template #default="{ row }">
              <el-link type="primary" underline="never" @click="open(row.summary.planId)">
                {{ truncate(row.summary.rawRequirement, 46) }}
              </el-link>
            </template>
          </el-table-column>
          <el-table-column label="迭代窗口" width="196">
            <template #default="{ row }">{{ row.summary.iterationStartDate }} ~ {{ row.summary.iterationDeadline }}</template>
          </el-table-column>
          <el-table-column label="开发人数" width="90" align="center">
            <template #default="{ row }">{{ row.summary.devMemberCount }}</template>
          </el-table-column>
          <el-table-column label="任务数" prop="summary.taskCount" width="80" align="center" />
          <el-table-column label="总工时" width="96" align="right">
            <template #default="{ row }">{{ row.summary.totalEstimatedHours }}h</template>
          </el-table-column>
          <el-table-column label="人均工时" width="100" align="right">
            <template #default="{ row }">{{ row.perCapitaHours.toFixed(1) }}h</template>
          </el-table-column>
          <el-table-column label="关键工期" width="112" align="right">
            <template #default="{ row }">{{ row.summary.totalDurationHours }}h</template>
          </el-table-column>
          <el-table-column label="负荷率" prop="loadRatio" width="112" align="right" sortable>
            <template #default="{ row }">
              <span :class="row.loadRatio > 1 ? 'ip-text-danger' : ''">{{ (row.loadRatio * 100).toFixed(1) }}%</span>
            </template>
          </el-table-column>
          <el-table-column label="剩余工作日" prop="remainingDays" width="110" align="center" sortable>
            <template #default="{ row }">
              <el-tag v-if="row.overdue" type="danger" size="small" effect="plain">已截止</el-tag>
              <span v-else>{{ row.remainingDays }} 天</span>
            </template>
          </el-table-column>
          <el-table-column label="风险" width="120" align="center">
            <template #default="{ row }">
              <el-tag v-if="row.summary.riskCount === 0" type="success" size="small" effect="plain">无</el-tag>
              <span v-else>
                <el-tag v-if="row.summary.highRiskCount > 0" type="danger" size="small" effect="dark">
                  高 {{ row.summary.highRiskCount }}
                </el-tag>
                <el-tag type="info" size="small" effect="plain" style="margin-left: 4px">{{ row.summary.riskCount }}</el-tag>
              </span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="90" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="open(row.summary.planId)">总览</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <div class="ip-panel">
        <div class="ip-panel__title"><span>口径说明</span></div>
        <ul class="ip-portfolio__notes">
          <li>「关键路径工期」「总工时」「风险」均直接取自引擎返回值，前端不做任何重算。</li>
          <li>「负荷率 = 预估总工时 / 可用总工时」，可用总工时按迭代窗口内工作日 × 8h × 开发人数推算，仅用于横向对比。</li>
          <li>「剩余工作日」按工作日（周一~周五）从今天推算至迭代截止日期；已过截止日期的计划标为「已截止」。</li>
        </ul>
      </div>
    </template>
  </div>
</template>

<style scoped>
.ip-page__actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.ip-metric-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 14px;
}

.ip-metric {
  display: grid;
  gap: 4px;
  padding: 12px 14px;
  border: 1px solid var(--ip-border);
  border-radius: var(--ip-radius);
  background: var(--ip-surface-sunk);
}

.ip-metric__label {
  font-family: var(--ip-font-mono);
  font-size: 11px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--ip-text-muted);
}

.ip-metric__value {
  font-family: var(--ip-font-display);
  font-size: 22px;
  font-weight: 700;
  color: var(--ip-brand);
}

.ip-metric__value.is-danger,
.ip-text-danger {
  color: var(--ip-danger);
}

.ip-portfolio__notes {
  margin: 0;
  padding-left: 18px;
  display: grid;
  gap: 6px;
  font-size: 12.5px;
  color: var(--ip-text-secondary);
  line-height: 1.7;
}
</style>

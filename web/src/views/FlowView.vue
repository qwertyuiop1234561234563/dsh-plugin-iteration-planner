<script setup lang="ts">
/**
 * 度量分析：累积流图（CFD）与速度图（Velocity）。
 *
 * ⚠️ 定位说明：
 *  - 本页**不产生任何业务口径**，只把引擎既有返回（计划快照 / 燃尽数据）与审计日志
 *    整理成图形；派生逻辑集中在 `utils/analytics.ts`，关键路径 / 工期 / 风险仍以引擎为准；
 *  - CFD 的基线口径见 `deriveCfd`：有过状态变迁的任务基线视为待办，其余取当前状态，
 *    末端用真实状态收口；
 *  - 速度图直接对引擎燃尽数据做逐日差分，工作日推进规则完全由引擎决定。
 */
import { computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import type { EChartsOption } from 'echarts'
import type { TaskStatus } from '@/api/types'
import EChart from '@/components/EChart.vue'
import { usePlanStore } from '@/stores/plan'
import { cfdStatuses, deriveCfd, deriveVelocity, velocitySummary } from '@/utils/analytics'
import { STATUS_COLOR, STATUS_LABEL, hoursToWorkDays } from '@/utils/format'
import { CHART_AXIS_LINE, CHART_MARK_LINE, CHART_SPLIT_LINE, CHART_TEXT, TOKEN } from '@/utils/palette'

const store = usePlanStore()
const route = useRoute()

const plan = computed(() => store.plan)

/** 累积流图数据点 */
const cfdPoints = computed(() => deriveCfd(plan.value, store.audit))

/** CFD 中实际出现过的状态（避免空系列） */
const cfdOrders = computed<TaskStatus[]>(() => cfdStatuses(cfdPoints.value))

/** 速度图数据点 */
const velocityPoints = computed(() => deriveVelocity(plan.value?.burndownRawData ?? []))

/** 速度摘要 */
const velocity = computed(() => velocitySummary(velocityPoints.value))

/** 累积流图（堆叠面积：自下而上 DONE → TODO，顶部带宽随进度收窄） */
const cfdOption = computed<EChartsOption>(() => {
  const points = cfdPoints.value
  const orders = cfdOrders.value
  return {
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'line', lineStyle: { color: TOKEN.slate, type: 'dashed' } },
      valueFormatter: (value: unknown) => `${String(value)} 项`,
    },
    legend: {
      bottom: 0,
      itemWidth: 12,
      itemHeight: 8,
      textStyle: CHART_TEXT,
      data: orders.map((status) => STATUS_LABEL[status]),
    },
    grid: { left: 48, right: 22, top: 18, bottom: 54 },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: points.map((point) => point.label),
      axisLabel: CHART_TEXT,
      axisLine: CHART_AXIS_LINE,
    },
    yAxis: {
      type: 'value',
      minInterval: 1,
      name: '任务数',
      nameTextStyle: CHART_TEXT,
      splitLine: CHART_SPLIT_LINE,
      axisLabel: CHART_TEXT,
    },
    series: orders.map((status) => ({
      name: STATUS_LABEL[status],
      type: 'line',
      stack: 'cfd',
      smooth: false,
      showSymbol: false,
      symbolSize: 6,
      areaStyle: { color: STATUS_COLOR[status], opacity: 0.82 },
      lineStyle: { width: 1, color: STATUS_COLOR[status] },
      itemStyle: { color: STATUS_COLOR[status] },
      emphasis: { focus: 'series' },
      data: points.map((point) => point.counts[status] ?? 0),
    })),
  }
})

/** 速度图（柱=当日完成工时，线=累计完成工时，虚线=每日均值参考） */
const velocityOption = computed<EChartsOption>(() => {
  const points = velocityPoints.value
  const avg = velocity.value.activeDays > 0 ? velocity.value.avgPerDay : 0
  return {
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      valueFormatter: (value: unknown) => `${String(value)} h`,
    },
    legend: {
      bottom: 0,
      itemWidth: 12,
      itemHeight: 8,
      textStyle: CHART_TEXT,
      data: ['当日完成', '累计完成'],
    },
    grid: { left: 52, right: 26, top: 18, bottom: 54 },
    xAxis: {
      type: 'category',
      data: points.map((point) => point.day),
      axisLabel: CHART_TEXT,
      axisLine: CHART_AXIS_LINE,
    },
    yAxis: {
      type: 'value',
      name: '工时(h)',
      nameTextStyle: CHART_TEXT,
      splitLine: CHART_SPLIT_LINE,
      axisLabel: CHART_TEXT,
    },
    series: [
      {
        name: '当日完成',
        type: 'bar',
        barWidth: 18,
        data: points.map((point) => point.completed),
        itemStyle: { color: TOKEN.accent, borderRadius: [2, 2, 0, 0] },
      },
      {
        name: '累计完成',
        type: 'line',
        smooth: true,
        showSymbol: true,
        symbolSize: 6,
        data: points.map((point) => point.cumulative),
        lineStyle: { width: 2, color: TOKEN.brand },
        itemStyle: { color: TOKEN.brand },
        markLine:
          avg > 0
            ? {
                silent: true,
                symbol: 'none',
                lineStyle: CHART_MARK_LINE,
                label: { formatter: `均值 ${avg}h/活跃日`, ...CHART_TEXT, position: 'insideEndTop' },
                data: [{ yAxis: avg }],
              }
            : undefined,
      },
    ],
  }
})

onMounted(async () => {
  const planId = route.params.planId as string | undefined
  if (planId && store.currentPlanId !== planId) await store.loadPlan(planId)
  await store.loadAudit(planId)
})
</script>

<template>
  <div class="ip-page">
    <el-empty v-if="!plan" description="请选择或新建一个迭代计划" />

    <template v-else>
      <div class="ip-panel">
        <div class="ip-panel__title">
          <span>度量摘要</span>
          <el-button :icon="'Refresh'" @click="store.refreshCurrent()">刷新</el-button>
        </div>
        <div class="ip-metric-grid">
          <div class="ip-metric">
            <span class="ip-metric__label">累计完成工时</span>
            <strong class="ip-metric__value">{{ velocity.totalCompleted }}h</strong>
            <span class="ip-muted">{{ hoursToWorkDays(velocity.totalCompleted) }}</span>
          </div>
          <div class="ip-metric">
            <span class="ip-metric__label">日均完成</span>
            <strong class="ip-metric__value">{{ velocity.avgPerDay }}h</strong>
            <span class="ip-muted">{{ velocity.activeDays }} 个活跃工作日</span>
          </div>
          <div class="ip-metric">
            <span class="ip-metric__label">剩余工时</span>
            <strong class="ip-metric__value">{{ velocity.remaining }}h</strong>
            <span class="ip-muted">当前未完成预估</span>
          </div>
          <div class="ip-metric">
            <span class="ip-metric__label">任务状态跨度</span>
            <strong class="ip-metric__value">{{ cfdOrders.length }} 种</strong>
            <span class="ip-muted">共 {{ plan.taskList.length }} 项任务</span>
          </div>
        </div>
      </div>

      <div class="ip-panel">
        <div class="ip-panel__title">
          <span>累积流图（CFD）</span>
          <span class="ip-muted" style="font-size: 12px">由审计状态变迁重建；末端以当前真实状态收口</span>
        </div>
        <EChart v-if="cfdPoints.length > 1" :option="cfdOption" height="340px" />
        <el-empty
          v-else
          description="暂无可用于重建累积流图的状态变迁记录（在任务管理页调整任务状态后自动生成）"
          :image-size="80"
        />
      </div>

      <div class="ip-panel">
        <div class="ip-panel__title">
          <span>速度图（Velocity）</span>
          <span class="ip-muted" style="font-size: 12px">对引擎燃尽数据逐日差分，口径完全由引擎决定</span>
        </div>
        <EChart v-if="velocityPoints.length > 0" :option="velocityOption" height="320px" />
        <el-empty v-else description="暂无燃尽数据（引擎在生成迭代报表时输出）" :image-size="80" />
      </div>

      <div class="ip-panel">
        <div class="ip-panel__title"><span>说明</span></div>
        <ul class="ip-flow__notes">
          <li>本页所有图形均为<strong>展示层派生</strong>，不参与排期、关键路径与风险判定；唯一事实来源是引擎 <code>src/**</code>。</li>
          <li>CFD 的工作流顺序为「已完成 → 测试中 → 进行中 → 阻塞 → 已延期 → 待办」（自下而上堆叠）。</li>
          <li>速度图的每日完成量取自相邻两日剩余工时之差，工作日推进规则（含缓冲修正）由引擎输出。</li>
        </ul>
      </div>
    </template>
  </div>
</template>

<style scoped>
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

.ip-flow__notes {
  margin: 0;
  padding-left: 18px;
  display: grid;
  gap: 6px;
  font-size: 12.5px;
  color: var(--ip-text-secondary);
  line-height: 1.7;
}

.ip-flow__notes code {
  font-family: var(--ip-font-mono);
  padding: 1px 4px;
  border-radius: 2px;
  background: var(--ip-surface-sunk);
}
</style>

<script setup lang="ts">
/**
 * 迭代仪表盘：KPI + 多维度分布图 + 责任人工时负载。
 *
 * 数据全部来自 store 对引擎返回值的派生（状态 / 类型 / 优先级 / 责任人聚合），
 * 纯粹的求和与计数用于展示，不参与任何业务判定；人员过载判定仍以引擎风险结果为权威，
 * 此处仅从引擎风险描述中解析责任人名称做高亮（见 store.overloadedMembers）。
 */
import { computed } from 'vue'
import type { EChartsOption } from 'echarts'
import type { Priority, ProjectPlan, TaskType } from '@/api/types'
import EChart from '@/components/EChart.vue'
import KpiCards from '@/components/KpiCards.vue'
import { usePlanStore } from '@/stores/plan'
import { PRIORITY_COLOR, RISK_COLOR, STATUS_LABEL, TASK_TYPE_COLOR, TASK_TYPE_LABEL } from '@/utils/format'
import {
  CHART_AXIS_LINE,
  CHART_EMPTY,
  CHART_SERIES,
  CHART_SPLIT_LINE,
  CHART_TEXT,
  TOKEN,
} from '@/utils/palette'

const store = usePlanStore()
const plan = computed<ProjectPlan | null>(() => store.plan)

/** 完成率（已完成任务占比，纯展示统计） */
const completion = computed(() => {
  const list = plan.value?.taskList ?? []
  if (list.length === 0) return 0
  const done = list.filter((task) => task.status === 'DONE').length
  return Math.round((done / list.length) * 100)
})

/** 任务状态分布（柱状） */
const statusOption = computed<EChartsOption>(() => {
  const items = store.statusStats
  return {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 48, right: 20, top: 20, bottom: 30 },
    xAxis: {
      type: 'category',
      data: items.map((item) => STATUS_LABEL[item.status as keyof typeof STATUS_LABEL] ?? item.status),
      axisLabel: CHART_TEXT,
      axisLine: CHART_AXIS_LINE,
    },
    yAxis: {
      type: 'value',
      minInterval: 1,
      splitLine: CHART_SPLIT_LINE,
      axisLabel: CHART_TEXT,
    },
    series: [
      {
        type: 'bar',
        barWidth: 26,
        data: items.map((item) => item.count),
        itemStyle: { color: CHART_SERIES, borderRadius: [2, 2, 0, 0] },
        label: { show: true, position: 'top', fontSize: 11, color: TOKEN.slate },
      },
    ],
  }
})

/** 优先级分布（环形） */
const priorityOption = computed<EChartsOption>(() => {
  const list = plan.value?.taskList ?? []
  const order: Priority[] = ['P0', 'P1', 'P2', 'P3']
  const data = order.map((priority) => ({
    name: priority,
    value: list.filter((task) => task.priority === priority).length,
    itemStyle: { color: PRIORITY_COLOR[priority] },
  }))
  return {
    tooltip: { trigger: 'item' },
    legend: { bottom: 0, itemWidth: 12, itemHeight: 8, textStyle: CHART_TEXT },
    series: [
      {
        type: 'pie',
        radius: ['46%', '68%'],
        center: ['50%', '44%'],
        avoidLabelOverlap: true,
        label: { formatter: '{b} {c}', ...CHART_TEXT },
        data: data.filter((item) => item.value > 0),
      },
    ],
  }
})

/** 任务类型分布（条形） */
const typeOption = computed<EChartsOption>(() => {
  const list = plan.value?.taskList ?? []
  const keys = [...new Set(list.map((task) => task.taskType))]
  const data = keys
    .map((type) => ({ type, count: list.filter((task) => task.taskType === type).length }))
    .sort((a, b) => b.count - a.count)
  return {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 96, right: 30, top: 16, bottom: 26 },
    xAxis: {
      type: 'value',
      minInterval: 1,
      splitLine: CHART_SPLIT_LINE,
      axisLabel: CHART_TEXT,
    },
    yAxis: {
      type: 'category',
      data: data.map((item) => TASK_TYPE_LABEL[item.type as TaskType]).reverse(),
      axisLabel: CHART_TEXT,
      axisLine: CHART_AXIS_LINE,
    },
    series: [
      {
        type: 'bar',
        barWidth: 16,
        data: data
          .map((item) => ({ value: item.count, itemStyle: { color: TASK_TYPE_COLOR[item.type as TaskType], borderRadius: [0, 2, 2, 0] } }))
          .reverse(),
        label: { show: true, position: 'right', fontSize: 11, color: TOKEN.slate },
      },
    ],
  }
})

/** 风险等级构成（环形） */
const riskOption = computed<EChartsOption>(() => {
  const stats = store.riskStats
  const data = [
    { name: 'HIGH', value: stats.high, itemStyle: { color: RISK_COLOR.HIGH } },
    { name: 'MEDIUM', value: stats.medium, itemStyle: { color: RISK_COLOR.MEDIUM } },
    { name: 'LOW', value: stats.low, itemStyle: { color: RISK_COLOR.LOW } },
  ].filter((item) => item.value > 0)
  return {
    tooltip: { trigger: 'item' },
    legend: { bottom: 0, itemWidth: 12, itemHeight: 8, textStyle: CHART_TEXT },
    series: [
      {
        type: 'pie',
        radius: ['46%', '68%'],
        center: ['50%', '44%'],
        label: { formatter: '{b} {c}', ...CHART_TEXT },
        data:
          data.length > 0
            ? data
            : [{ name: '无风险', value: 1, itemStyle: { color: CHART_EMPTY }, label: { color: TOKEN.muted } }],
      },
    ],
  }
})

/** 责任人工时负载（条形；过载责任人红色高亮，来源为引擎风险结果） */
const resourceOption = computed<EChartsOption>(() => {
  const rows = [...store.resourceLoad].reverse()
  return {
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      valueFormatter: (value: unknown) => `${String(value)} h`,
    },
    grid: { left: 96, right: 46, top: 16, bottom: 26 },
    xAxis: {
      type: 'value',
      name: '工时(h)',
      nameTextStyle: CHART_TEXT,
      splitLine: CHART_SPLIT_LINE,
      axisLabel: CHART_TEXT,
    },
    yAxis: {
      type: 'category',
      data: rows.map((row) => row.assignee),
      axisLabel: CHART_TEXT,
      axisLine: CHART_AXIS_LINE,
    },
    series: [
      {
        type: 'bar',
        barWidth: 16,
        data: rows.map((row) => ({
          value: Math.round(row.hours * 100) / 100,
          itemStyle: {
            color: store.overloadedMembers.has(row.assignee)
              ? TOKEN.danger
              : row.criticalCount > 0
                ? TOKEN.brand
                : TOKEN.accent,
            borderRadius: [0, 2, 2, 0],
          },
        })),
        label: { show: true, position: 'right', formatter: '{c} h', fontSize: 11, color: TOKEN.slate },
      },
    ],
  }
})

const overloadNames = computed(() => [...store.overloadedMembers])
</script>

<template>
  <div class="ip-page">
    <el-empty v-if="!plan" description="请选择或新建一个迭代计划" />

    <template v-else>
      <div class="ip-panel">
        <div class="ip-panel__title">
          <span>进度概览</span>
          <el-button :icon="'Refresh'" @click="store.refreshCurrent()">刷新</el-button>
        </div>
        <div class="ip-progress">
          <el-progress type="dashboard" :percentage="completion" :width="132" :stroke-width="10" />
          <div class="ip-progress__meta">
            <div>
              <span class="ip-muted">已完成</span>
              <strong>{{ plan.taskList.filter((t) => t.status === 'DONE').length }}</strong> / {{ plan.taskList.length }}
            </div>
            <div>
              <span class="ip-muted">计划完成日期</span>
              <strong>{{ store.plannedEndDate ?? '—' }}</strong>
            </div>
            <div>
              <span class="ip-muted">关键路径工期</span>
              <strong>{{ plan.totalDurationHours }}h</strong>
              <span class="ip-muted">（{{ store.durationWorkDays }} 个工作日）</span>
            </div>
            <div v-if="overloadNames.length > 0" class="ip-progress__alert">
              <el-icon><WarningFilled /></el-icon>
              引擎检出人员过载：{{ overloadNames.join('、') }}
            </div>
          </div>
        </div>
      </div>

      <KpiCards :plan="plan" />

      <div class="ip-grid ip-grid--2">
        <div class="ip-panel">
          <div class="ip-panel__title"><span>任务状态分布</span></div>
          <EChart :option="statusOption" height="240px" />
        </div>
        <div class="ip-panel">
          <div class="ip-panel__title"><span>优先级分布</span></div>
          <EChart :option="priorityOption" height="240px" />
        </div>
      </div>

      <div class="ip-grid ip-grid--2">
        <div class="ip-panel">
          <div class="ip-panel__title"><span>任务类型分布</span></div>
          <EChart :option="typeOption" height="240px" />
        </div>
        <div class="ip-panel">
          <div class="ip-panel__title">
            <span>风险等级构成</span>
            <el-tag size="small" effect="plain">共 {{ store.riskStats.total }} 条</el-tag>
          </div>
          <EChart :option="riskOption" height="240px" />
        </div>
      </div>

      <div class="ip-panel">
        <div class="ip-panel__title">
          <span>责任人工时负载</span>
          <span class="ip-muted" style="font-size: 12px">按责任人聚合预估工时；红色为引擎判定的人员过载</span>
        </div>
        <EChart :option="resourceOption" :height="`${Math.max(200, store.resourceLoad.length * 34 + 60)}px`" />
      </div>
    </template>
  </div>
</template>

<style scoped>
.ip-progress {
  display: flex;
  align-items: center;
  gap: 28px;
  flex-wrap: wrap;
}

.ip-progress__meta {
  display: grid;
  gap: 10px;
  font-size: 13px;
}

.ip-progress__meta strong {
  margin-left: 8px;
  font-size: 15px;
  color: var(--ip-text);
}

.ip-progress__alert {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--ip-danger);
  font-size: 12.5px;
}
</style>
<script setup lang="ts">
/**
 * 燃尽增强分析：把「引擎的理想燃尽线」与「由审计重建的实际剩余曲线」叠加对比。
 *
 * 口径边界：
 *  - 理想线为引擎 `burndownRawData`（工作日推进的唯一事实来源）；
 *  - 实际线由服务端按审计记录逐事件重建（`GET /workspace/plans/:id/burndown-analysis`），
 *    属**展示性重建**，不参与引擎排期与风险判定；
 *  - 偏离度 / 燃尽速率 / 预计完成日均由服务端推导，前端仅做展示与按「天」对齐两条曲线。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import type { EChartsOption } from 'echarts'
import { ApiClientError, api } from '@/api/client'
import type { BurndownAnalysis } from '@/api/types'
import EChart from '@/components/EChart.vue'
import StatTiles, { type StatTile } from '@/components/StatTiles.vue'
import { usePlanStore } from '@/stores/plan'
import {
  CHART_AXIS_LINE,
  CHART_MARK_LINE,
  CHART_SERIES,
  CHART_SPLIT_LINE,
  CHART_TEXT,
  TOKEN,
} from '@/utils/palette'
import { STATUS_LABEL } from '@/utils/format'

const store = usePlanStore()
const route = useRoute()

const planId = computed(() => (route.params.planId as string | undefined) ?? store.currentPlanId)

const result = ref<BurndownAnalysis | null>(null)
const loading = ref(false)
const errorText = ref('')

/** ISO 时间 / 日期 → 天键（两套曲线按「天」对齐：理想线本就是按工作日给出的日期） */
function dayKey(time: string): string {
  return time.slice(0, 10)
}

/** 合并后的天数轴（理想线与实际线的天并集，按日期升序） */
const days = computed<string[]>(() => {
  const data = result.value
  if (!data) return []
  const set = new Set<string>()
  for (const point of data.ideal) set.add(dayKey(point.time))
  for (const point of data.actual) set.add(dayKey(point.time))
  return [...set].sort()
})

/** 按天取值：同一天多次采样取最后一条（实际曲线同一工作日可能有多次状态变更） */
function seriesByDay(list: Array<{ time: string; remainingHours: number }>): Array<number | null> {
  const map = new Map<string, number>()
  for (const point of list) map.set(dayKey(point.time), point.remainingHours)
  return days.value.map((day) => (map.has(day) ? map.get(day)! : null))
}

const chartOption = computed<EChartsOption>(() => {
  const data = result.value
  const labels = days.value
  return {
    tooltip: { trigger: 'axis', valueFormatter: (value: unknown) => (value === null || value === undefined ? '—' : `${String(value)} h`) },
    legend: { data: ['实际剩余（审计重建）', '理想燃尽（引擎）'], right: 10, top: 0, itemWidth: 14, itemHeight: 8, textStyle: CHART_TEXT },
    grid: { left: 52, right: 22, top: 36, bottom: 46 },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: labels,
      axisLine: CHART_AXIS_LINE,
      axisLabel: { ...CHART_TEXT, rotate: labels.length > 12 ? 40 : 0 },
    },
    yAxis: {
      type: 'value',
      name: '剩余工时(h)',
      nameTextStyle: CHART_TEXT,
      splitLine: CHART_SPLIT_LINE,
      axisLabel: CHART_TEXT,
    },
    series: [
      {
        name: '实际剩余（审计重建）',
        type: 'line',
        smooth: true,
        symbolSize: 6,
        connectNulls: true,
        data: data ? seriesByDay(data.actual) : [],
        itemStyle: { color: CHART_SERIES },
        lineStyle: { color: CHART_SERIES, width: 2 },
      },
      {
        name: '理想燃尽（引擎）',
        type: 'line',
        smooth: false,
        symbol: 'none',
        connectNulls: true,
        data: data ? seriesByDay(data.ideal) : [],
        lineStyle: CHART_MARK_LINE,
      },
      {
        name: '计划完成日',
        type: 'line',
        data: [],
        // 计划完成日：以引擎理想线末点为参考的垂直批注线
        markLine:
          data && data.ideal.length > 0
            ? {
                silent: true,
                symbol: 'none',
                label: {
                  formatter: `计划完成 ${data.ideal[data.ideal.length - 1].time}`,
                  color: CHART_TEXT.color,
                  fontFamily: CHART_TEXT.fontFamily,
                },
                lineStyle: { color: TOKEN.accent, type: 'dashed', width: 1.5 },
                data: [{ xAxis: dayKey(data.ideal[data.ideal.length - 1].time) }],
              }
            : undefined,
      },
    ],
  }
})

/** 当前剩余（实际曲线末点） */
const currentRemaining = computed(() => {
  const points = result.value?.actual ?? []
  return points.length > 0 ? points[points.length - 1].remainingHours : null
})

/** 末端状态构成（末点 statusCounts 转可读文案） */
const endStatuses = computed(() => {
  const points = result.value?.actual ?? []
  const counts = points.length > 0 ? points[points.length - 1].statusCounts : {}
  return Object.entries(counts)
    .filter(([, value]) => value > 0)
    .map(([status, value]) => `${STATUS_LABEL[status as keyof typeof STATUS_LABEL] ?? status} ${value}`)
    .join(' · ')
})

/** 状态构成明细（在脚本侧聚合，避免模板内遍历对象时丢失键的类型信息） */
function statusEntries(counts: Record<string, number> | undefined): Array<{ status: string; label: string; count: number }> {
  return Object.entries(counts ?? {})
    .filter(([, count]) => count > 0)
    .map(([status, count]) => ({ status, label: STATUS_LABEL[status as keyof typeof STATUS_LABEL] ?? status, count }))
}

const tiles = computed<StatTile[]>(() => {
  const data = result.value
  if (!data) return []
  const onTrack = data.onTrack
  return [
    { label: '当前剩余工时', value: currentRemaining.value ?? '—', unit: 'h', tone: 'brand' },
    { label: '燃尽速率', value: data.burnRatePerDay ?? '—', unit: 'h/天' },
    {
      label: '偏离度（相对理想线）',
      value: data.deviationHours ?? '—',
      unit: 'h',
      tone: data.deviationHours === null ? 'default' : data.deviationHours > 0 ? 'danger' : 'success',
      hint: data.deviationHours === null ? '样本不足' : data.deviationHours > 0 ? '落后于理想线' : '领先于理想线',
    },
    { label: '预计完成日', value: data.projectedEndDate ?? '—' },
    {
      label: '进度判定',
      value: onTrack === null ? '无法判定' : onTrack ? '按计划' : '存在延期风险',
      tone: onTrack === null ? 'default' : onTrack ? 'success' : 'danger',
      hint: data.actual.length > 0 && data.ideal.length > 0 ? `计划完成 ${data.ideal[data.ideal.length - 1].time}` : undefined,
    },
    { label: '审计样本', value: data.recordCount, unit: '条' },
  ]
})

async function load(): Promise<void> {
  if (!planId.value) return
  loading.value = true
  errorText.value = ''
  try {
    result.value = await api.burndownAnalysis(planId.value)
  } catch (error) {
    result.value = null
    errorText.value = error instanceof ApiClientError ? error.message : String(error)
    ElMessage.error(`加载燃尽分析失败：${errorText.value}`)
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(planId, () => {
  result.value = null
  void load()
})
</script>

<template>
  <div class="ip-page">
    <el-empty v-if="!planId" description="请选择或新建一个迭代计划" />

    <template v-else>
      <StatTiles :items="tiles" :columns="6" />

      <el-alert v-if="errorText" type="error" :closable="false">{{ errorText }}</el-alert>

      <div class="ip-panel" v-loading="loading">
        <div class="ip-panel__title">
          <span>实际剩余 vs 理想燃尽</span>
          <div class="ip-burndown__actions">
            <el-tag v-if="endStatuses" size="small" effect="plain">末端状态：{{ endStatuses }}</el-tag>
            <el-button size="small" :icon="'Refresh'" @click="load">刷新</el-button>
          </div>
        </div>
        <EChart v-if="result && days.length > 0" :option="chartOption" height="360px" />
        <el-empty v-else description="暂无燃尽数据（计划可能为空或引擎未生成燃尽序列）" />
      </div>

      <div class="ip-panel">
        <div class="ip-panel__title">
          <span>实际剩余曲线数据点（审计重建）</span>
          <el-tag size="small" effect="plain">{{ result?.actual.length ?? 0 }} 个采样点</el-tag>
        </div>
        <el-table :data="result?.actual ?? []" size="small" border max-height="360">
          <el-table-column label="时间" width="180">
            <template #default="{ row }"><span class="ip-mono">{{ row.time }}</span></template>
          </el-table-column>
          <el-table-column label="剩余工时(h)" width="120" align="right">
            <template #default="{ row }"><span class="ip-mono">{{ row.remainingHours }}</span></template>
          </el-table-column>
          <el-table-column label="状态构成">
            <template #default="{ row }">
              <span class="ip-muted">
                <span v-for="entry in statusEntries(row.statusCounts)" :key="entry.status" class="ip-burndown__status">
                  {{ entry.label }} {{ entry.count }}
                </span>
              </span>
            </template>
          </el-table-column>
          <template #empty><el-empty description="暂无采样点" /></template>
        </el-table>
      </div>
    </template>
  </div>
</template>

<style scoped>
.ip-burndown__actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.ip-burndown__status {
  margin-right: 10px;
  font-size: 12px;
}
</style>

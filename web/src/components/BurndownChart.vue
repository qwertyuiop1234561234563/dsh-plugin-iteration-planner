<script setup lang="ts">
/**
 * 燃尽图组件（ECharts）：实际剩余工时曲线 + 理想燃尽参考线。
 *
 * 说明：理想线以「总预估工时 → 0」在时间节点上线性插值，便于判断进度是否偏离预期。
 */
import * as echarts from 'echarts'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { BurndownItem } from '@/api/types'

const props = defineProps<{
  data: BurndownItem[]
  /** 理想燃尽起点（通常为总预估工时） */
  totalHours: number
}>()

const container = ref<HTMLDivElement>()
let chart: echarts.ECharts | null = null

function render(): void {
  if (!chart || !container.value) return
  const points = props.data ?? []
  const labels = points.map((item) => item.time)
  const actual = points.map((item) => item.remainingHours)
  const start = props.totalHours > 0 ? props.totalHours : actual[0] ?? 0
  const step = points.length > 1 ? start / (points.length - 1) : start
  const ideal = points.map((_item, index) => Math.max(0, Math.round((start - step * index) * 100) / 100))

  chart.setOption(
    {
      tooltip: { trigger: 'axis', valueFormatter: (value: unknown) => `${String(value)} h` },
      legend: { data: ['实际剩余', '理想燃尽'], right: 10, top: 0, itemWidth: 14, itemHeight: 8 },
      grid: { left: 48, right: 18, top: 36, bottom: 34 },
      xAxis: {
        type: 'category',
        boundaryGap: false,
        data: labels,
        axisLine: { lineStyle: { color: '#dcdfe6' } },
        axisLabel: { color: '#8a94a6', fontSize: 11 },
      },
      yAxis: {
        type: 'value',
        name: '剩余工时(h)',
        nameTextStyle: { color: '#8a94a6', fontSize: 11 },
        splitLine: { lineStyle: { color: '#eef1f6' } },
        axisLabel: { color: '#8a94a6', fontSize: 11 },
      },
      series: [
        {
          name: '实际剩余',
          type: 'line',
          smooth: true,
          symbolSize: 6,
          data: actual,
          itemStyle: { color: '#2468f2' },
          areaStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: 'rgba(36,104,242,0.28)' },
              { offset: 1, color: 'rgba(36,104,242,0.02)' },
            ]),
          },
        },
        {
          name: '理想燃尽',
          type: 'line',
          smooth: false,
          symbol: 'none',
          data: ideal,
          lineStyle: { type: 'dashed', color: '#98a2b3', width: 1.5 },
        },
      ],
    },
    true,
  )
}

function resize(): void {
  chart?.resize()
}

onMounted(() => {
  if (!container.value) return
  chart = echarts.init(container.value)
  render()
  window.addEventListener('resize', resize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', resize)
  chart?.dispose()
  chart = null
})

watch(() => [props.data, props.totalHours], render, { deep: true })
</script>

<template>
  <div ref="container" class="ip-burndown" />
</template>

<style scoped>
.ip-burndown {
  width: 100%;
  height: 300px;
}
</style>
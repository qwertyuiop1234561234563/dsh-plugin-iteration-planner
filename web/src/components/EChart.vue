<script setup lang="ts">
/**
 * 通用 ECharts 容器：负责实例初始化 / 自适应 / 销毁，只接收一个 `option`。
 *
 * 抽出来的原因：仪表盘需要多张图（状态 / 类型 / 优先级 / 资源负载 / 风险构成），
 * 复用同一生命周期管理可避免在每个图里重复 init / resize / dispose 逻辑。
 */
import * as echarts from 'echarts'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = withDefaults(
  defineProps<{
    option: echarts.EChartsOption
    height?: string
  }>(),
  { height: '260px' },
)

const container = ref<HTMLDivElement>()
let chart: echarts.ECharts | null = null

function render(): void {
  chart?.setOption(props.option, true)
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

watch(() => props.option, render, { deep: true })
</script>

<template>
  <div ref="container" :style="{ width: '100%', height }" />
</template>
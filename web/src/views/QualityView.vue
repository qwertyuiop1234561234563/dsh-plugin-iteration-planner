<script setup lang="ts">
/**
 * 交付质量看板：把「引擎计划状态」与「治理台账（人工风险登记册）」聚合成质量指标。
 *
 * 口径边界：
 *  - 完成率 / 延期占比 / 阻塞任务均取自引擎返回的任务状态；
 *  - 按时完成率仅统计「有 DONE 状态变迁记录且计划完成日可知」的任务（服务端给出样本数）；
 *  - 阻塞时长以审计中最后一次进入 BLOCKED 的时间为起点重建，属**展示性重建**；
 *  - 缺陷密度 = 未关闭人工风险数 / 任务总数 × 100（每百任务口径）。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import type { EChartsOption } from 'echarts'
import { ApiClientError, api } from '@/api/client'
import type { QualityResult } from '@/api/types'
import EChart from '@/components/EChart.vue'
import StatTiles, { type StatTile } from '@/components/StatTiles.vue'
import { usePlanStore } from '@/stores/plan'
import { exportCsv, fileStamp } from '@/utils/export'
import { BLOCKED_EXPORT_COLUMNS } from '@/utils/exportPresets'
import { STATUS_COLOR, STATUS_LABEL, formatIso } from '@/utils/format'
import { CHART_AXIS_LINE, CHART_MARK_LINE, CHART_SERIES, CHART_SPLIT_LINE, CHART_TEXT, TOKEN } from '@/utils/palette'

const store = usePlanStore()
const route = useRoute()

const planId = computed(() => (route.params.planId as string | undefined) ?? store.currentPlanId)

const result = ref<QualityResult | null>(null)
const loading = ref(false)
const errorText = ref('')

/** 百分比展示：0~1 → 百分比文本，null 表示无样本 */
function percent(rate: number | null, sample?: number): string {
  if (rate === null) return sample === 0 ? '无样本' : '—'
  return `${Math.round(rate * 1000) / 10}%`
}

const tiles = computed<StatTile[]>(() => {
  const data = result.value
  if (!data) return []
  return [
    { label: '任务总数', value: data.taskTotal, unit: '项' },
    { label: '完成率', value: percent(data.doneRate), tone: 'brand', hint: `已完成 ${data.doneCount} 项` },
    {
      label: '按时完成率',
      value: percent(data.onTimeRate, data.onTimeSample),
      tone: data.onTimeRate === null ? 'default' : data.onTimeRate >= 1 ? 'success' : 'warning',
      hint: `样本 ${data.onTimeSample} 项`,
    },
    {
      label: '延期占比',
      value: percent(data.delayedRate),
      tone: data.delayedCount > 0 ? 'danger' : 'success',
      hint: `延期 ${data.delayedCount} 项`,
    },
    {
      label: '阻塞任务',
      value: data.blockedCount,
      unit: '项',
      tone: data.blockedCount > 0 ? 'warning' : 'success',
      hint: `累计阻塞 ${data.totalBlockedHours}h`,
    },
    {
      label: '缺陷密度',
      value: data.issueDensity,
      hint: `未关闭人工风险 ${data.registerOpenCount} 项 / 百任务`,
      tone: data.registerHighOpenCount > 0 ? 'danger' : 'default',
    },
  ]
})

/** 状态构成饼图（引擎任务状态） */
const statusOption = computed<EChartsOption>(() => {
  const counts = result.value?.statusCounts ?? {}
  const data = Object.entries(counts)
    .filter(([, value]) => value > 0)
    .map(([status, value]) => ({
      name: STATUS_LABEL[status as keyof typeof STATUS_LABEL] ?? status,
      value,
      itemStyle: { color: STATUS_COLOR[status as keyof typeof STATUS_COLOR] ?? TOKEN.slate },
    }))
  return {
    tooltip: { trigger: 'item', formatter: '{b}：{c} 项（{d}%）' },
    legend: { bottom: 0, itemWidth: 12, itemHeight: 8, textStyle: CHART_TEXT },
    series: [
      {
        type: 'pie',
        radius: ['42%', '68%'],
        center: ['50%', '46%'],
        avoidLabelOverlap: true,
        data,
        label: { color: CHART_TEXT.color, fontFamily: CHART_TEXT.fontFamily, fontSize: 11 },
      },
    ],
  }
})

/** 关键质量指标条形（完成率 / 按时完成率 / 延期占比，均为百分比） */
const rateOption = computed<EChartsOption>(() => {
  const data = result.value
  const value = (rate: number | null): number | null => (rate === null ? null : Math.round(rate * 1000) / 10)
  return {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, valueFormatter: (v: unknown) => (v === null ? '无样本' : `${String(v)}%`) },
    grid: { left: 78, right: 30, top: 20, bottom: 26 },
    xAxis: { type: 'value', max: 100, name: '%', nameTextStyle: CHART_TEXT, splitLine: CHART_SPLIT_LINE, axisLabel: CHART_TEXT },
    yAxis: {
      type: 'category',
      inverse: true,
      data: ['完成率', '按时完成率', '延期占比'],
      axisLine: CHART_AXIS_LINE,
      axisLabel: CHART_TEXT,
    },
    series: [
      {
        type: 'bar',
        barMaxWidth: 20,
        data: [
          { value: value(data?.doneRate ?? 0), itemStyle: { color: CHART_SERIES } },
          { value: value(data?.onTimeRate ?? null), itemStyle: { color: TOKEN.success } },
          { value: value(data?.delayedRate ?? 0), itemStyle: { color: TOKEN.danger } },
        ],
        markLine: {
          silent: true,
          symbol: 'none',
          label: { formatter: '100%', color: CHART_TEXT.color, fontFamily: CHART_TEXT.fontFamily },
          lineStyle: { color: CHART_MARK_LINE.color, type: 'dashed', width: CHART_MARK_LINE.width },
          data: [{ xAxis: 100 }],
        },
      },
    ],
  }
})

async function load(): Promise<void> {
  if (!planId.value) return
  loading.value = true
  errorText.value = ''
  try {
    result.value = await api.quality(planId.value)
  } catch (error) {
    result.value = null
    errorText.value = error instanceof ApiClientError ? error.message : String(error)
    ElMessage.error(`加载交付质量指标失败：${errorText.value}`)
  } finally {
    loading.value = false
  }
}

function exportBlocked(): void {
  const tasks = result.value?.blockedTasks ?? []
  if (tasks.length === 0) {
    ElMessage.warning('当前没有阻塞任务可导出')
    return
  }
  exportCsv(`blocked-tasks-${fileStamp()}.csv`, BLOCKED_EXPORT_COLUMNS, tasks)
  ElMessage.success('阻塞任务清单已导出')
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

      <div class="ip-grid ip-grid--2">
        <div class="ip-panel" v-loading="loading">
          <div class="ip-panel__title"><span>任务状态构成</span></div>
          <EChart v-if="result && result.taskTotal > 0" :option="statusOption" height="300px" />
          <el-empty v-else description="当前计划没有任务" />
        </div>

        <div class="ip-panel" v-loading="loading">
          <div class="ip-panel__title">
            <span>关键质量指标</span>
            <span class="ip-muted ip-quality__tip">按时完成率仅统计有状态变迁记录且计划完成日可知的任务</span>
          </div>
          <EChart v-if="result && result.taskTotal > 0" :option="rateOption" height="300px" />
          <el-empty v-else description="暂无质量指标样本" />
        </div>
      </div>

      <div class="ip-grid ip-grid--2">
        <div class="ip-panel" v-loading="loading">
          <div class="ip-panel__title">
            <span>阻塞任务清单</span>
            <div class="ip-quality__actions">
              <el-tag size="small" effect="plain" type="warning">累计阻塞 {{ result?.totalBlockedHours ?? 0 }}h</el-tag>
              <el-button size="small" :icon="'Download'" :disabled="(result?.blockedTasks.length ?? 0) === 0" @click="exportBlocked">
                导出阻塞任务
              </el-button>
            </div>
          </div>
          <el-table :data="result?.blockedTasks ?? []" size="small" border max-height="300">
            <el-table-column label="任务名称" min-width="180">
              <template #default="{ row }">{{ row.taskName ?? row.taskId }}</template>
            </el-table-column>
            <el-table-column label="进入阻塞时间" width="170">
              <template #default="{ row }">
                <span class="ip-mono ip-muted">{{ row.since ? formatIso(row.since) : '未知（无状态变迁记录）' }}</span>
              </template>
            </el-table-column>
            <el-table-column label="阻塞时长(h)" width="116" align="right">
              <template #default="{ row }">
                <span class="ip-mono">{{ row.blockedHours === null ? '—' : row.blockedHours }}</span>
              </template>
            </el-table-column>
            <template #empty><el-empty description="当前没有阻塞任务" /></template>
          </el-table>
        </div>

        <div class="ip-panel" v-loading="loading">
          <div class="ip-panel__title"><span>风险与缺陷台账</span></div>
          <div class="ip-quality__stats">
            <div class="ip-quality__stat">
              <span class="ip-label">引擎风险</span>
              <span class="ip-quality__value">{{ result?.engineRiskCount ?? 0 }}</span>
              <span class="ip-muted">高风险 {{ result?.engineHighRiskCount ?? 0 }} 条</span>
            </div>
            <div class="ip-quality__stat">
              <span class="ip-label">人工登记</span>
              <span class="ip-quality__value">{{ result?.registerTotal ?? 0 }}</span>
              <span class="ip-muted">未关闭 {{ result?.registerOpenCount ?? 0 }} 条</span>
            </div>
            <div class="ip-quality__stat" :class="{ 'is-alert': (result?.registerHighOpenCount ?? 0) > 0 }">
              <span class="ip-label">高危未关闭</span>
              <span class="ip-quality__value">{{ result?.registerHighOpenCount ?? 0 }}</span>
              <span class="ip-muted">严重度 HIGH / CRITICAL</span>
            </div>
            <div class="ip-quality__stat">
              <span class="ip-label">缺陷密度</span>
              <span class="ip-quality__value">{{ result?.issueDensity ?? 0 }}</span>
              <span class="ip-muted">每百任务未关闭人工风险</span>
            </div>
          </div>
          <p class="ip-muted ip-quality__note">
            计划完成日（引擎燃尽末点）：{{ result?.plannedEndDate ?? '—' }}。
            人工风险条目在「风险登记册」页维护，此处仅聚合展示。
          </p>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.ip-quality__tip {
  font-size: 12px;
  font-weight: 400;
}

.ip-quality__actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.ip-quality__stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 12px;
}

.ip-quality__stat {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 12px 14px;
  border: 1px solid var(--ip-border);
  border-left: 3px solid var(--ip-brand);
  border-radius: var(--ip-radius);
  background: var(--ip-surface-sunk);
}

.ip-quality__stat.is-alert {
  border-left-color: var(--ip-danger);
  background: color-mix(in srgb, var(--ip-danger) 8%, transparent);
}

.ip-quality__value {
  font-family: var(--ip-font-mono);
  font-size: 22px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  color: var(--ip-text);
}

.ip-quality__note {
  margin: 14px 0 0;
  font-size: 12px;
  line-height: 1.7;
}
</style>

<script setup lang="ts">
/**
 * 人员负荷与资源平衡：按责任人聚合预估工时并与「迭代可用容量」对比。
 *
 * 口径边界：
 *  - 容量 = 迭代可用工作日 × 每日工时，两者均由服务端从**引擎燃尽数据**与统一常量得出，
 *    前端不做任何工期换算；
 *  - 过载判定由服务端给出（`overloaded`），前端只负责着色与告警；
 *  - 待分配任务池为「无责任人」的只读清单，用于资源再分配时的人工决策。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import type { EChartsOption } from 'echarts'
import { ApiClientError, api } from '@/api/client'
import type { WorkloadResult } from '@/api/types'
import EChart from '@/components/EChart.vue'
import StatTiles, { type StatTile } from '@/components/StatTiles.vue'
import { usePlanStore } from '@/stores/plan'
import { exportCsv, fileStamp } from '@/utils/export'
import { UNASSIGNED_EXPORT_COLUMNS, WORKLOAD_EXPORT_COLUMNS } from '@/utils/exportPresets'
import { ASSIGNEE_PALETTE, CHART_AXIS_LINE, CHART_MARK_LINE, CHART_SPLIT_LINE, CHART_TEXT, TOKEN } from '@/utils/palette'

const store = usePlanStore()
const route = useRoute()

const planId = computed(() => (route.params.planId as string | undefined) ?? store.currentPlanId)

const result = ref<WorkloadResult | null>(null)
const loading = ref(false)
const errorText = ref('')

const tiles = computed<StatTile[]>(() => {
  const data = result.value
  if (!data) return []
  return [
    { label: '参与成员', value: data.totals.memberCount, unit: '人' },
    { label: '人均容量', value: data.capacityHoursPerMember, unit: 'h', hint: `${data.planWorkdays} 工作日 × ${data.hoursPerDay}h` },
    { label: '已分配工时', value: data.totals.allocatedHours, unit: 'h', tone: 'brand' },
    {
      label: '超载成员',
      value: data.totals.overloadedMembers.length,
      unit: '人',
      tone: data.totals.overloadedMembers.length > 0 ? 'danger' : 'success',
      hint: data.totals.overloadedMembers.length > 0 ? data.totals.overloadedMembers.join('、') : '无',
    },
    { label: '待分配任务', value: data.unassigned.taskCount, unit: '项', tone: data.unassigned.taskCount > 0 ? 'warning' : 'default', hint: `${data.unassigned.estimatedHours}h` },
  ]
})

/** 负荷对比图：预估工时 / 剩余工时 分组柱 + 容量基准线 */
const chartOption = computed<EChartsOption>(() => {
  const data = result.value
  const members = data?.members ?? []
  return {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, valueFormatter: (value: unknown) => `${String(value)} h` },
    legend: { data: ['预估工时', '剩余工时'], right: 10, top: 0, itemWidth: 14, itemHeight: 8, textStyle: CHART_TEXT },
    grid: { left: 88, right: 24, top: 36, bottom: 26 },
    xAxis: {
      type: 'value',
      name: '工时(h)',
      nameTextStyle: CHART_TEXT,
      splitLine: CHART_SPLIT_LINE,
      axisLabel: CHART_TEXT,
    },
    yAxis: {
      type: 'category',
      inverse: true,
      data: members.map((member) => member.assignee),
      axisLine: CHART_AXIS_LINE,
      axisLabel: CHART_TEXT,
    },
    series: [
      {
        name: '预估工时',
        type: 'bar',
        barMaxWidth: 16,
        data: members.map((member, index) => ({
          value: member.estimatedHours,
          itemStyle: { color: member.overloaded ? TOKEN.danger : ASSIGNEE_PALETTE[index % ASSIGNEE_PALETTE.length] },
        })),
      },
      {
        name: '剩余工时',
        type: 'bar',
        barMaxWidth: 16,
        data: members.map((member) => member.remainingHours),
        itemStyle: { color: TOKEN.ink300 },
      },
      {
        name: '容量基准',
        type: 'bar',
        data: [],
        // 人均容量基准线（虚线批注），横轴值轴上的垂直参考线
        markLine: {
          silent: true,
          symbol: 'none',
          label: { formatter: `容量 ${data?.capacityHoursPerMember ?? 0}h`, color: CHART_TEXT.color, fontFamily: CHART_TEXT.fontFamily },
          lineStyle: { color: CHART_MARK_LINE.color, type: 'dashed', width: CHART_MARK_LINE.width },
          data: [{ xAxis: data?.capacityHoursPerMember ?? 0 }],
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
    result.value = await api.workload(planId.value)
  } catch (error) {
    result.value = null
    errorText.value = error instanceof ApiClientError ? error.message : String(error)
    ElMessage.error(`加载人员负荷失败：${errorText.value}`)
  } finally {
    loading.value = false
  }
}

/** 负荷率（0~1）→ 进度条百分比（超 100% 截断展示，超载状态由服务端给出） */
function loadPercent(ratio: number): number {
  return Math.min(100, Math.round(ratio * 100))
}

function exportMembers(): void {
  const members = result.value?.members ?? []
  if (members.length === 0) {
    ElMessage.warning('暂无成员负荷数据可导出')
    return
  }
  exportCsv(`workload-${fileStamp()}.csv`, WORKLOAD_EXPORT_COLUMNS, members)
  ElMessage.success('人员负荷已导出')
}

function exportUnassigned(): void {
  const tasks = result.value?.unassigned.tasks ?? []
  if (tasks.length === 0) {
    ElMessage.warning('暂无待分配任务可导出')
    return
  }
  exportCsv(`unassigned-${fileStamp()}.csv`, UNASSIGNED_EXPORT_COLUMNS, tasks)
  ElMessage.success('待分配任务池已导出')
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
      <StatTiles :items="tiles" :columns="5" />

      <el-alert
        v-if="result && result.totals.overloadedMembers.length > 0"
        type="warning"
        :closable="false"
        show-icon
        title="存在超载成员"
      >
        {{ result.totals.overloadedMembers.join('、') }} 的预估工时已超过迭代可用容量（{{ result.capacityHoursPerMember }}h），建议拆分任务或调整责任人。
      </el-alert>

      <el-alert v-if="errorText" type="error" :closable="false">{{ errorText }}</el-alert>

      <div class="ip-panel">
        <div class="ip-panel__title">
          <span>负荷与容量对比</span>
          <div class="ip-workload__actions">
            <el-button size="small" :icon="'Refresh'" @click="load">刷新</el-button>
            <el-button size="small" :icon="'Download'" :disabled="(result?.members.length ?? 0) === 0" @click="exportMembers">导出成员负荷</el-button>
          </div>
        </div>
        <EChart v-if="result && result.members.length > 0" :option="chartOption" height="320px" />
        <el-empty v-else description="当前计划没有已分配责任人的任务" />
      </div>

      <div class="ip-panel">
        <div class="ip-panel__title">
          <span>成员负荷明细</span>
          <el-tag size="small" effect="plain">{{ result?.members.length ?? 0 }} 人</el-tag>
        </div>
        <el-table v-loading="loading" :data="result?.members ?? []" size="small" border>
          <el-table-column label="责任人" min-width="140">
            <template #default="{ row }">
              <div class="ip-workload__name">
                <span>{{ row.assignee }}</span>
                <el-tag v-if="row.overloaded" size="small" type="danger" effect="plain">超载</el-tag>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="任务数" width="90" align="center">
            <template #default="{ row }">
              <span class="ip-mono">{{ row.taskCount }}</span>
              <span v-if="row.criticalCount > 0" class="ip-muted ip-workload__critical">关键 {{ row.criticalCount }}</span>
            </template>
          </el-table-column>
          <el-table-column label="预估工时(h)" width="116" align="right">
            <template #default="{ row }"><span class="ip-mono">{{ row.estimatedHours }}</span></template>
          </el-table-column>
          <el-table-column label="剩余工时(h)" width="116" align="right">
            <template #default="{ row }"><span class="ip-mono">{{ row.remainingHours }}</span></template>
          </el-table-column>
          <el-table-column label="可用容量(h)" width="116" align="right">
            <template #default="{ row }"><span class="ip-mono">{{ row.capacityHours }}</span></template>
          </el-table-column>
          <el-table-column label="负荷率" min-width="180">
            <template #default="{ row }">
              <el-progress
                :percentage="loadPercent(row.loadRatio)"
                :status="row.overloaded ? 'exception' : undefined"
                :stroke-width="10"
                :format="() => `${Math.round(row.loadRatio * 100)}%`"
              />
            </template>
          </el-table-column>
          <template #empty><el-empty description="暂无成员负荷数据" /></template>
        </el-table>
      </div>

      <div class="ip-panel">
        <div class="ip-panel__title">
          <span>待分配任务池</span>
          <div class="ip-workload__actions">
            <el-tag size="small" effect="plain" type="warning">
              {{ result?.unassigned.taskCount ?? 0 }} 项 · {{ result?.unassigned.estimatedHours ?? 0 }}h
            </el-tag>
            <el-button size="small" :icon="'Download'" :disabled="(result?.unassigned.tasks.length ?? 0) === 0" @click="exportUnassigned">
              导出待分配
            </el-button>
          </div>
        </div>
        <el-table :data="result?.unassigned.tasks ?? []" size="small" border max-height="360">
          <el-table-column label="任务ID" width="180">
            <template #default="{ row }"><span class="ip-mono ip-muted">{{ row.taskId }}</span></template>
          </el-table-column>
          <el-table-column label="任务名称" min-width="220">
            <template #default="{ row }">
              <div class="ip-workload__name">
                <span>{{ row.taskName }}</span>
                <el-tag v-if="row.critical" size="small" type="danger" effect="plain">关键</el-tag>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="工时(h)" width="90" align="right">
            <template #default="{ row }"><span class="ip-mono">{{ row.estimatedHours }}</span></template>
          </el-table-column>
          <el-table-column label="优先级" width="90" align="center">
            <template #default="{ row }">{{ row.priority ?? '—' }}</template>
          </el-table-column>
          <template #empty><el-empty description="所有任务均已指派责任人" /></template>
        </el-table>
      </div>
    </template>
  </div>
</template>

<style scoped>
.ip-workload__actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.ip-workload__name {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
  color: var(--ip-text);
}

.ip-workload__critical {
  display: block;
  font-size: 11px;
}
</style>

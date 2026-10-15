<script setup lang="ts">
/**
 * 基线快照与偏差对比：为当前计划保存命名基线，并与现状做「增删任务 + 字段变更 + 指标偏差」对比。
 *
 * 口径说明：快照内容由**服务端从引擎读取当前计划**后固化（不接受前端上传计划体），
 * 因此对比结果中的工期 / 关键路径 / 风险均来自引擎在其各自时点的计算结果，前端不做任何重算。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ApiClientError, api } from '@/api/client'
import type { BaselineDetail, BaselineDiff, BaselineSummary } from '@/api/types'
import StatTiles, { type StatTile } from '@/components/StatTiles.vue'
import { usePlanStore } from '@/stores/plan'
import { exportCsv, fileStamp } from '@/utils/export'
import {
  BASELINE_DIFF_EXPORT_COLUMNS,
  BASELINE_EXPORT_COLUMNS,
  flattenBaselineDiff,
} from '@/utils/exportPresets'
import { STATUS_LABEL, UPDATABLE_FIELD_LABEL, formatIso } from '@/utils/format'

const store = usePlanStore()
const route = useRoute()

const planId = computed(() => (route.params.planId as string | undefined) ?? store.currentPlanId)

const items = ref<BaselineSummary[]>([])
const loading = ref(false)
const errorText = ref('')
const selectedId = ref('')
const detail = ref<BaselineDetail | null>(null)
const diff = ref<BaselineDiff | null>(null)
const diffLoading = ref(false)

const formName = ref('')
const formNote = ref('')
const creating = ref(false)

/** 偏差对比指标口径与文案（键名与后端返回值一致） */
const METRIC_LABELS: Record<string, string> = {
  taskCount: '任务数',
  criticalTaskCount: '关键路径任务数',
  riskCount: '风险总数',
  highRiskCount: '高风险数',
  totalEstimatedHours: '总工时(h)',
  totalDurationHours: '关键工期(h)',
  devMemberCount: '开发人数',
  plannedEndDate: '计划完成日',
}
const METRIC_ORDER = ['taskCount', 'criticalTaskCount', 'riskCount', 'highRiskCount', 'totalEstimatedHours', 'totalDurationHours', 'devMemberCount', 'plannedEndDate']

const metricRows = computed(() => {
  if (!diff.value) return []
  return METRIC_ORDER.filter((key) => diff.value!.metrics[key]).map((key) => ({
    key,
    label: METRIC_LABELS[key] ?? key,
    ...diff.value!.metrics[key],
  }))
})

const summaryTiles = computed<StatTile[]>(() => {
  const current = items.value.find((item) => item.baselineId === selectedId.value)
  if (!current) return []
  return [
    { label: '任务数', value: current.taskCount },
    { label: '关键路径任务数', value: current.criticalTaskCount, tone: 'brand' },
    { label: '总工时', value: current.totalEstimatedHours, unit: 'h' },
    { label: '关键工期', value: current.totalDurationHours, unit: 'h' },
    {
      label: '风险',
      value: current.highRiskCount > 0 ? `${current.riskCount}（高 ${current.highRiskCount}）` : current.riskCount,
      tone: current.highRiskCount > 0 ? 'danger' : 'default',
    },
    { label: '计划完成日', value: current.plannedEndDate ?? '—' },
  ]
})

async function loadList(): Promise<void> {
  if (!planId.value) return
  loading.value = true
  errorText.value = ''
  try {
    const result = await api.listBaselines(planId.value)
    items.value = result.items
    if (selectedId.value && !items.value.some((item) => item.baselineId === selectedId.value)) {
      selectedId.value = ''
      diff.value = null
      detail.value = null
    }
  } catch (error) {
    items.value = []
    errorText.value = error instanceof ApiClientError ? error.message : String(error)
    ElMessage.error(`加载基线失败：${errorText.value}`)
  } finally {
    loading.value = false
  }
}

async function create(): Promise<void> {
  if (!planId.value) return
  const name = formName.value.trim()
  if (!name) {
    ElMessage.warning('请填写基线名称')
    return
  }
  creating.value = true
  try {
    const created = await api.createBaseline(planId.value, { name, note: formNote.value.trim() || undefined })
    formName.value = ''
    formNote.value = ''
    ElMessage.success(`基线已保存：${created.name}`)
    await loadList()
    await select(created.baselineId)
  } catch (error) {
    ElMessage.error(`保存基线失败：${error instanceof ApiClientError ? error.message : String(error)}`)
  } finally {
    creating.value = false
  }
}

async function select(baselineId: string): Promise<void> {
  if (!planId.value) return
  selectedId.value = baselineId
  diffLoading.value = true
  diff.value = null
  detail.value = null
  try {
    const [diffResult, detailResult] = await Promise.all([
      api.baselineDiff(planId.value, baselineId),
      api.getBaseline(planId.value, baselineId),
    ])
    diff.value = diffResult
    detail.value = detailResult
  } catch (error) {
    ElMessage.error(`加载偏差对比失败：${error instanceof ApiClientError ? error.message : String(error)}`)
  } finally {
    diffLoading.value = false
  }
}

async function remove(baselineId: string, name: string): Promise<void> {
  if (!planId.value) return
  try {
    await ElMessageBox.confirm(`确认删除基线「${name}」？该操作不可撤销。`, '删除基线', { type: 'warning' })
  } catch {
    return
  }
  try {
    await api.deleteBaseline(planId.value, baselineId)
    ElMessage.success('基线已删除')
    if (selectedId.value === baselineId) {
      selectedId.value = ''
      diff.value = null
      detail.value = null
    }
    await loadList()
  } catch (error) {
    ElMessage.error(`删除基线失败：${error instanceof ApiClientError ? error.message : String(error)}`)
  }
}

function exportList(): void {
  exportCsv(`baselines-${fileStamp()}.csv`, BASELINE_EXPORT_COLUMNS, items.value)
  ElMessage.success('基线列表已导出')
}

function exportDiff(): void {
  if (!diff.value) return
  exportCsv(`baseline-diff-${fileStamp()}.csv`, BASELINE_DIFF_EXPORT_COLUMNS, flattenBaselineDiff(diff.value, METRIC_LABELS))
  ElMessage.success('偏差对比已导出')
}

/** 字段值展示（枚举字段用统一映射） */
function fieldValue(field: string, value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  if (field === 'status') return STATUS_LABEL[value as keyof typeof STATUS_LABEL] ?? String(value)
  if (field === 'estimatedHours') return `${value} h`
  if (field === 'predecessorTaskIds') return Array.isArray(value) && value.length > 0 ? value.join(' / ') : '（无）'
  return String(value)
}

function fieldLabel(field: string): string {
  return UPDATABLE_FIELD_LABEL[field as keyof typeof UPDATABLE_FIELD_LABEL] ?? field
}

/** 偏差数值着色：正偏差用信号色，负偏差用成功色（仅展示语义） */
function deltaTone(delta: number | null): string {
  if (delta === null || delta === 0) return ''
  return delta > 0 ? 'ip-baseline__delta--up' : 'ip-baseline__delta--down'
}

onMounted(loadList)
watch(planId, () => {
  selectedId.value = ''
  diff.value = null
  detail.value = null
  void loadList()
})
</script>

<template>
  <div class="ip-page">
    <el-empty v-if="!planId" description="请选择或新建一个迭代计划" />

    <template v-else>
      <div class="ip-panel">
        <div class="ip-panel__title">
          <span>保存基线快照</span>
          <span class="ip-muted ip-baseline__tip">快照由服务端读取引擎当前计划后固化，用于后续偏差追溯</span>
        </div>
        <div class="ip-baseline__form">
          <el-input v-model="formName" placeholder="基线名称（如：迭代启动基线 / 提测前基线）" style="max-width: 320px" />
          <el-input v-model="formNote" placeholder="备注（可选）" style="max-width: 320px" />
          <el-button type="primary" :loading="creating" :icon="'Camera'" @click="create">保存当前计划为基线</el-button>
          <el-button :icon="'Refresh'" @click="loadList">刷新</el-button>
          <el-button :icon="'Download'" :disabled="items.length === 0" @click="exportList">导出基线列表</el-button>
        </div>
      </div>

      <el-alert v-if="errorText" type="error" :closable="false">{{ errorText }}</el-alert>

      <div class="ip-grid ip-grid--2">
        <div class="ip-panel">
          <div class="ip-panel__title"><span>基线列表</span><el-tag size="small" effect="plain">{{ items.length }} 个</el-tag></div>
          <el-table v-loading="loading" :data="items" size="small" border highlight-current-row @row-click="(row: BaselineSummary) => select(row.baselineId)">
            <el-table-column label="名称" min-width="160">
              <template #default="{ row }">
                <div class="ip-baseline__name">{{ row.name }}</div>
                <div class="ip-muted ip-baseline__note">{{ row.note || '—' }}</div>
              </template>
            </el-table-column>
            <el-table-column label="创建时间" width="170">
              <template #default="{ row }"><span class="ip-mono">{{ formatIso(row.createdAt) }}</span></template>
            </el-table-column>
            <el-table-column label="任务" width="64" align="center">
              <template #default="{ row }">{{ row.taskCount }}</template>
            </el-table-column>
            <el-table-column label="工期(h)" width="80" align="right">
              <template #default="{ row }"><span class="ip-mono">{{ row.totalDurationHours }}</span></template>
            </el-table-column>
            <el-table-column label="操作" width="120" align="center">
              <template #default="{ row }">
                <el-button size="small" text type="primary" @click.stop="select(row.baselineId)">对比</el-button>
                <el-button size="small" text type="danger" @click.stop="remove(row.baselineId, row.name)">删除</el-button>
              </template>
            </el-table-column>
            <template #empty>
              <el-empty description="尚未保存任何基线，可先在上方保存当前计划" />
            </template>
          </el-table>
        </div>

        <div class="ip-panel" v-loading="diffLoading">
          <div class="ip-panel__title">
            <span>偏差对比</span>
            <el-button v-if="diff" size="small" :icon="'Download'" @click="exportDiff">导出对比</el-button>
          </div>
          <el-empty v-if="!diff" description="从左侧选择一个基线开始对比" />
          <template v-else>
            <StatTiles :items="summaryTiles" :columns="3" />
            <el-table :data="metricRows" size="small" border class="ip-baseline__metrics">
              <el-table-column label="指标" min-width="130">
                <template #default="{ row }">{{ row.label }}</template>
              </el-table-column>
              <el-table-column label="基线" align="right" min-width="100">
                <template #default="{ row }"><span class="ip-mono">{{ row.from ?? '—' }}</span></template>
              </el-table-column>
              <el-table-column label="现状" align="right" min-width="100">
                <template #default="{ row }"><span class="ip-mono">{{ row.to ?? '—' }}</span></template>
              </el-table-column>
              <el-table-column label="偏差" align="right" min-width="100">
                <template #default="{ row }">
                  <span class="ip-mono" :class="deltaTone(row.delta)">{{ row.delta === null ? '—' : row.delta }}</span>
                </template>
              </el-table-column>
            </el-table>
          </template>
        </div>
      </div>

      <template v-if="diff">
        <div class="ip-grid ip-grid--3">
          <div class="ip-panel">
            <div class="ip-panel__title"><span>新增任务</span><el-tag size="small" effect="plain" type="success">{{ diff.added.length }}</el-tag></div>
            <el-table :data="diff.added" size="small" border max-height="280">
              <el-table-column label="任务" min-width="150">
                <template #default="{ row }">{{ row.taskName }}</template>
              </el-table-column>
              <el-table-column label="工时(h)" width="80" align="right">
                <template #default="{ row }"><span class="ip-mono">{{ row.estimatedHours ?? '—' }}</span></template>
              </el-table-column>
              <el-table-column label="状态" width="90">
                <template #default="{ row }">{{ STATUS_LABEL[row.status as keyof typeof STATUS_LABEL] ?? row.status ?? '—' }}</template>
              </el-table-column>
              <template #empty><el-empty description="无新增任务" /></template>
            </el-table>
          </div>

          <div class="ip-panel">
            <div class="ip-panel__title"><span>删除任务</span><el-tag size="small" effect="plain" type="warning">{{ diff.removed.length }}</el-tag></div>
            <el-table :data="diff.removed" size="small" border max-height="280">
              <el-table-column label="任务" min-width="150">
                <template #default="{ row }">{{ row.taskName }}</template>
              </el-table-column>
              <el-table-column label="工时(h)" width="80" align="right">
                <template #default="{ row }"><span class="ip-mono">{{ row.estimatedHours ?? '—' }}</span></template>
              </el-table-column>
              <el-table-column label="状态" width="90">
                <template #default="{ row }">{{ STATUS_LABEL[row.status as keyof typeof STATUS_LABEL] ?? row.status ?? '—' }}</template>
              </el-table-column>
              <template #empty><el-empty description="无删除任务" /></template>
            </el-table>
          </div>

          <div class="ip-panel">
            <div class="ip-panel__title"><span>字段变更</span><el-tag size="small" effect="plain">{{ diff.changed.length }}</el-tag></div>
            <el-table :data="diff.changed" size="small" border max-height="280">
              <el-table-column label="任务" min-width="130">
                <template #default="{ row }">{{ row.taskName }}</template>
              </el-table-column>
              <el-table-column label="变更明细" min-width="220">
                <template #default="{ row }">
                  <div v-for="field in row.fields" :key="field.field" class="ip-baseline__change">
                    <span class="ip-muted">{{ fieldLabel(field.field) }}</span>
                    <span class="ip-baseline__from">{{ fieldValue(field.field, field.from) }}</span>
                    <span class="ip-muted">→</span>
                    <span class="ip-baseline__to">{{ fieldValue(field.field, field.to) }}</span>
                  </div>
                </template>
              </el-table-column>
              <template #empty><el-empty description="无字段变更" /></template>
            </el-table>
          </div>
        </div>
      </template>

      <div v-if="detail" class="ip-panel">
        <div class="ip-panel__title">
          <span>基线任务清单</span>
          <span class="ip-muted ip-baseline__tip">{{ detail.name }} · 共 {{ detail.taskList.length }} 个任务（快照时点）</span>
        </div>
        <el-table :data="detail.taskList" size="small" border max-height="360">
          <el-table-column label="任务名称" min-width="180" prop="taskName" />
          <el-table-column label="状态" width="90">
            <template #default="{ row }">{{ STATUS_LABEL[row.status as keyof typeof STATUS_LABEL] ?? row.status }}</template>
          </el-table-column>
          <el-table-column label="优先级" width="80" prop="priority" />
          <el-table-column label="责任人" width="110">
            <template #default="{ row }">{{ row.assignee ?? '待分配' }}</template>
          </el-table-column>
          <el-table-column label="工时(h)" width="80" align="right" prop="estimatedHours" />
          <el-table-column label="松弛(h)" width="80" align="right" prop="slack" />
        </el-table>
      </div>
    </template>
  </div>
</template>

<style scoped>
.ip-baseline__tip {
  font-size: 12px;
  font-weight: 400;
}

.ip-baseline__form {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.ip-baseline__name {
  font-weight: 600;
  color: var(--ip-text);
}

.ip-baseline__note {
  font-size: 12px;
}

.ip-baseline__metrics {
  margin-top: 12px;
}

.ip-baseline__delta--up {
  color: var(--ip-warning);
}

.ip-baseline__delta--down {
  color: var(--ip-success);
}

.ip-baseline__change {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  font-family: var(--ip-font-mono);
  flex-wrap: wrap;
}

.ip-baseline__from {
  color: var(--ip-text-muted);
  text-decoration: line-through;
}

.ip-baseline__to {
  color: var(--ip-text);
  font-weight: 600;
}
</style>

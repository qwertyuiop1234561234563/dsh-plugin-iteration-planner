<script setup lang="ts">
/**
 * What-if 情景对比：以「同一份需求 + 不同约束（起始日期 / 截止日期 / 开发人数）」重新规划，
 * 与当前基线计划并排对比关键指标。
 *
 * 实现说明：对比由后端 `/api/scenario/compare` 完成 —— 它调用引擎按相同需求临时生成一份
 * 计划、计算指标后**立即回收**该临时计划，因此不会污染基线计划，也无需前端复刻任何算法。
 */
import { computed, onMounted, reactive, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { usePlanStore } from '@/stores/plan'
import { hoursToWorkDayCount } from '@/utils/workday'

const store = usePlanStore()

const form = reactive({ iterationStartDate: '', iterationDeadline: '', devMemberCount: 1 })

/** 以当前计划为基线预填表单 */
function syncFromPlan(): void {
  const current = store.plan
  if (!current) return
  form.iterationStartDate = current.iterationStartDate
  form.iterationDeadline = current.iterationDeadline
  form.devMemberCount = current.devMemberCount
}

onMounted(syncFromPlan)
watch(() => store.plan?.planId, syncFromPlan)

async function run(): Promise<void> {
  if (!store.plan) return
  if (form.iterationDeadline < form.iterationStartDate) {
    ElMessage.warning('迭代截止日期不能早于起始日期')
    return
  }
  await store.runScenarioCompare({
    iterationStartDate: form.iterationStartDate,
    iterationDeadline: form.iterationDeadline,
    devMemberCount: form.devMemberCount,
  })
}

const result = computed(() => store.scenario)

/** 数值指标行（含变化方向：down 表示越小越好） */
interface MetricRow {
  label: string
  baseline: string
  scenario: string
  deltaValue: number
  unit: string
  direction: 'down' | 'up' | 'neutral'
}

const rows = computed<MetricRow[]>(() => {
  const data = result.value
  if (!data) return []
  const { baseline, scenario, delta } = data
  return [
    {
      label: '关键路径工期',
      baseline: `${baseline.totalDurationHours} h`,
      scenario: `${scenario.totalDurationHours} h`,
      deltaValue: delta.totalDurationHours,
      unit: ' h',
      direction: 'down',
    },
    {
      label: '折算工作日',
      baseline: `${hoursToWorkDayCount(baseline.totalDurationHours)} 天`,
      scenario: `${hoursToWorkDayCount(scenario.totalDurationHours)} 天`,
      deltaValue: delta.totalDurationHours,
      unit: ' h',
      direction: 'down',
    },
    {
      label: '总预估工时',
      baseline: `${baseline.totalEstimatedHours} h`,
      scenario: `${scenario.totalEstimatedHours} h`,
      deltaValue: delta.totalEstimatedHours,
      unit: ' h',
      direction: 'neutral',
    },
    {
      label: '任务总数',
      baseline: `${baseline.taskCount}`,
      scenario: `${scenario.taskCount}`,
      deltaValue: delta.taskCount,
      unit: '',
      direction: 'neutral',
    },
    {
      label: '关键任务数',
      baseline: `${baseline.criticalTaskCount}`,
      scenario: `${scenario.criticalTaskCount}`,
      deltaValue: delta.criticalTaskCount,
      unit: '',
      direction: 'neutral',
    },
    {
      label: '风险条目',
      baseline: `${baseline.riskCount}`,
      scenario: `${scenario.riskCount}`,
      deltaValue: delta.riskCount,
      unit: '',
      direction: 'down',
    },
    {
      label: '高危风险',
      baseline: `${baseline.highRiskCount}`,
      scenario: `${scenario.highRiskCount}`,
      deltaValue: delta.highRiskCount,
      unit: '',
      direction: 'down',
    },
  ]
})

function deltaText(value: number, unit: string): string {
  if (Math.abs(value) < 1e-6) return '无变化'
  const rounded = Math.round(value * 100) / 100
  return `${rounded > 0 ? '+' : ''}${rounded}${unit}`
}

function deltaClass(value: number, direction: 'down' | 'up' | 'neutral'): string {
  if (Math.abs(value) < 1e-6) return 'is-flat'
  if (direction === 'neutral') return 'is-neutral'
  const improved = direction === 'down' ? value < 0 : value > 0
  return improved ? 'is-better' : 'is-worse'
}

function constraintChanged(current: number | string, base: number | string): boolean {
  return String(current) !== String(base)
}
</script>

<template>
  <div class="ip-page">
    <el-empty v-if="!store.plan" description="请选择或新建一个迭代计划" />

    <template v-else>
      <div class="ip-panel">
        <div class="ip-panel__title">
          <span>What-if 情景对比</span>
          <el-button :icon="'Refresh'" @click="syncFromPlan">载入当前约束</el-button>
        </div>
        <el-alert
          type="info"
          :closable="false"
          show-icon
          title="按相同需求以新约束临时生成一份计划并与当前基线对比；临时计划在后端即时回收，当前计划不受影响。"
        />

        <el-form :inline="true" class="ip-whatif-form" label-width="88px">
          <el-form-item label="迭代起始">
            <el-date-picker v-model="form.iterationStartDate" type="date" value-format="YYYY-MM-DD" style="width: 168px" />
          </el-form-item>
          <el-form-item label="迭代截止">
            <el-date-picker v-model="form.iterationDeadline" type="date" value-format="YYYY-MM-DD" style="width: 168px" />
          </el-form-item>
          <el-form-item label="开发人数">
            <el-input-number v-model="form.devMemberCount" :min="1" :max="100" />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :icon="'DataAnalysis'" :loading="store.scenarioLoading" @click="run">
              开始对比
            </el-button>
          </el-form-item>
        </el-form>

        <div class="ip-whatif-hint ip-muted">
          基线：{{ store.plan.iterationStartDate }} ~ {{ store.plan.iterationDeadline }} · {{ store.plan.devMemberCount }} 人
          <template v-if="constraintChanged(form.devMemberCount, store.plan.devMemberCount)">
            → 情景 {{ form.devMemberCount }} 人
          </template>
        </div>
      </div>

      <el-empty v-if="!result && !store.scenarioLoading" description="调整约束后点击「开始对比」查看差异" />

      <template v-if="result">
        <div class="ip-grid ip-grid--2">
          <div class="ip-panel ip-whatif-card">
            <div class="ip-panel__title">
              <span>基线计划</span>
              <el-tag size="small" effect="plain" type="info">当前</el-tag>
            </div>
            <div class="ip-whatif-card__end">{{ result.baseline.plannedEndDate ?? '—' }}</div>
            <div class="ip-muted">计划完成日期</div>
            <div class="ip-whatif-card__meta">
              <span>{{ result.baselineConstraints.iterationStartDate }} ~ {{ result.baselineConstraints.iterationDeadline }}</span>
              <el-divider direction="vertical" />
              <span>{{ result.baselineConstraints.devMemberCount }} 人</span>
              <el-tag
                v-if="result.baseline.deadlineMet !== null"
                size="small"
                :type="result.baseline.deadlineMet ? 'success' : 'danger'"
                effect="plain"
                style="margin-left: 8px"
              >
                {{ result.baseline.deadlineMet ? '满足截止' : '超出截止' }}
              </el-tag>
            </div>
          </div>

          <div class="ip-panel ip-whatif-card ip-whatif-card--scenario">
            <div class="ip-panel__title">
              <span>情景计划</span>
              <el-tag size="small" effect="plain" type="primary">假设</el-tag>
            </div>
            <div class="ip-whatif-card__end">{{ result.scenario.plannedEndDate ?? '—' }}</div>
            <div class="ip-muted">计划完成日期</div>
            <div class="ip-whatif-card__meta">
              <span>{{ result.scenarioConstraints.iterationStartDate }} ~ {{ result.scenarioConstraints.iterationDeadline }}</span>
              <el-divider direction="vertical" />
              <span>{{ result.scenarioConstraints.devMemberCount }} 人</span>
              <el-tag
                v-if="result.scenario.deadlineMet !== null"
                size="small"
                :type="result.scenario.deadlineMet ? 'success' : 'danger'"
                effect="plain"
                style="margin-left: 8px"
              >
                {{ result.scenario.deadlineMet ? '满足截止' : '超出截止' }}
              </el-tag>
            </div>
          </div>
        </div>

        <div class="ip-panel">
          <div class="ip-panel__title"><span>指标差异（情景 − 基线）</span></div>
          <el-table :data="rows" stripe>
            <el-table-column prop="label" label="指标" min-width="160" />
            <el-table-column prop="baseline" label="基线" width="140" align="right" />
            <el-table-column prop="scenario" label="情景" width="140" align="right" />
            <el-table-column label="变化" width="150" align="right">
              <template #default="{ row }">
                <span class="ip-whatif-delta" :class="deltaClass(row.deltaValue, row.direction)">
                  {{ deltaText(row.deltaValue, row.unit) }}
                </span>
              </template>
            </el-table-column>
          </el-table>
          <div class="ip-whatif-legend ip-muted">
            <span class="ip-whatif-delta is-better">改善</span>
            <span class="ip-whatif-delta is-worse">恶化</span>
            <span class="ip-whatif-delta is-neutral">规模变化（中性）</span>
          </div>
        </div>
      </template>
    </template>
  </div>
</template>

<style scoped>
.ip-whatif-form {
  margin-top: 16px;
}

.ip-whatif-hint {
  font-size: 12.5px;
}

.ip-whatif-card--scenario {
  border-color: rgba(36, 104, 242, 0.35);
}

.ip-whatif-card__end {
  font-size: 24px;
  font-weight: 700;
  margin: 4px 0 2px;
}

.ip-whatif-card__meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  margin-top: 10px;
  font-size: 12.5px;
  color: var(--ip-text-secondary);
}

.ip-whatif-delta {
  font-weight: 600;
  font-size: 13px;
}

.ip-whatif-delta.is-better {
  color: var(--ip-success);
}

.ip-whatif-delta.is-worse {
  color: var(--ip-danger);
}

.ip-whatif-delta.is-neutral {
  color: var(--ip-brand);
}

.ip-whatif-delta.is-flat {
  color: var(--ip-text-secondary);
  font-weight: 500;
}

.ip-whatif-legend {
  display: flex;
  gap: 16px;
  margin-top: 12px;
  font-size: 12px;
}
</style>
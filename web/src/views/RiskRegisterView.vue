<script setup lang="ts">
/**
 * 风险登记册：把「引擎自动检出的风险」与「人工登记的风险」合并成一个工作视图。
 *
 * 口径边界：
 *  - 引擎风险（等级 / 关联任务 / 描述 / 缓解建议）直接取自计划快照，属只读；
 *  - 人工风险（严重度 / 概率 / 责任人 / 缓解措施 / 处置状态）由本页维护，
 *    持久化在服务端治理存储中，与引擎判定**互不覆盖**，仅在同一列表内并列展示；
 *  - 两者都不参与引擎的排期与风险判定，属治理台账性质。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ApiClientError, api } from '@/api/client'
import type { RegisterItem, RegisterPayload } from '@/api/types'
import {
  ISSUE_HIGH_SEVERITIES,
  ISSUE_OPEN_STATUSES,
  ISSUE_PROBABILITY_VALUES,
  ISSUE_SEVERITY_VALUES,
  ISSUE_STATUS_VALUES,
} from '@/api/types'
import StatTiles, { type StatTile } from '@/components/StatTiles.vue'
import { usePlanStore } from '@/stores/plan'
import { exportCsv, fileStamp } from '@/utils/export'
import { REGISTER_EXPORT_COLUMNS, RISK_EXPORT_COLUMNS } from '@/utils/exportPresets'
import {
  ISSUE_PROBABILITY_LABEL,
  ISSUE_SEVERITY_LABEL,
  ISSUE_SEVERITY_TAG,
  ISSUE_STATUS_LABEL,
  ISSUE_STATUS_TAG,
  RISK_LABEL,
  RISK_TAG,
  formatIso,
} from '@/utils/format'

const store = usePlanStore()
const route = useRoute()

const planId = computed(() => (route.params.planId as string | undefined) ?? store.currentPlanId)

/** 合并行（引擎风险 + 人工风险），用 source 区分来源 */
interface RiskRow {
  key: string
  source: 'engine' | 'manual'
  title: string
  severity: string
  severityLabel: string
  severityTag: 'primary' | 'success' | 'info' | 'warning' | 'danger'
  taskIds: string[]
  detail: string
  mitigation: string
  owner: string
  status: string
  statusLabel: string
  statusTag?: 'primary' | 'success' | 'info' | 'warning' | 'danger'
  time: string
  issue?: RegisterItem
}

const items = ref<RegisterItem[]>([])
const loading = ref(false)
const saving = ref(false)
const errorText = ref('')

const filterSource = ref<'all' | 'engine' | 'manual'>('all')
const filterSeverity = ref('')
const filterStatus = ref('')
const keyword = ref('')

/** 引擎风险行 */
const engineRows = computed<RiskRow[]>(() =>
  (store.plan?.riskList ?? []).map((risk) => ({
    key: `engine-${risk.riskId}`,
    source: 'engine',
    title: risk.description,
    severity: risk.level,
    severityLabel: RISK_LABEL[risk.level] ?? risk.level,
    severityTag: RISK_TAG[risk.level] ?? 'info',
    taskIds: risk.taskIds,
    detail: risk.suggest,
    mitigation: risk.suggest,
    owner: '',
    // 引擎风险只读，无处置状态；默认按「未关闭」的首个状态展示
    status: ISSUE_OPEN_STATUSES[0],
    statusLabel: ISSUE_STATUS_LABEL[ISSUE_OPEN_STATUSES[0]],
    time: '',
  })),
)

/** 人工风险行 */
const manualRows = computed<RiskRow[]>(() =>
  items.value.map((item) => ({
    key: `manual-${item.issueId}`,
    source: 'manual',
    title: item.title,
    severity: item.severity,
    severityLabel: `${ISSUE_SEVERITY_LABEL[item.severity]} / 概率 ${ISSUE_PROBABILITY_LABEL[item.probability]}`,
    severityTag: ISSUE_SEVERITY_TAG[item.severity],
    taskIds: item.linkedTaskIds,
    detail: item.description ?? '',
    mitigation: item.mitigation ?? '',
    owner: item.owner ?? '',
    status: item.status,
    statusLabel: ISSUE_STATUS_LABEL[item.status],
    statusTag: ISSUE_STATUS_TAG[item.status],
    time: item.updatedAt,
    issue: item,
  })),
)

const rows = computed<RiskRow[]>(() => {
  const lowered = keyword.value.trim().toLowerCase()
  const all = [...engineRows.value, ...manualRows.value]
  return all.filter((row) => {
    if (filterSource.value !== 'all' && row.source !== filterSource.value) return false
    if (filterSeverity.value && row.severity !== filterSeverity.value) return false
    if (filterStatus.value && row.status !== filterStatus.value) return false
    if (lowered) {
      const haystack = `${row.title} ${row.detail} ${row.mitigation} ${row.owner}`.toLowerCase()
      if (!haystack.includes(lowered)) return false
    }
    return true
  })
})

const tiles = computed<StatTile[]>(() => {
  const engineHigh = engineRows.value.filter((row) => row.severity === 'HIGH').length
  const manualOpen = items.value.filter((item) => ISSUE_OPEN_STATUSES.includes(item.status))
  const manualHigh = manualOpen.filter((item) => ISSUE_HIGH_SEVERITIES.includes(item.severity))
  return [
    { label: '引擎风险', value: engineRows.value.length, hint: `高等级 ${engineHigh} 条` },
    { label: '人工登记', value: items.value.length, tone: 'brand' },
    {
      label: '未关闭人工风险',
      value: manualOpen.length,
      tone: manualOpen.length > 0 ? 'warning' : 'success',
      hint: `高危 ${manualHigh.length} 条`,
    },
    {
      label: '合并视图',
      value: rows.value.length,
      hint: filterSource.value === 'all' ? '引擎 + 人工' : filterSource.value === 'engine' ? '仅引擎' : '仅人工',
    },
  ]
})

/* -------------------------------- 表单状态 -------------------------------- */

const dialogVisible = ref(false)
const editingId = ref('')
const form = ref<RegisterPayload>({
  title: '',
  description: '',
  severity: 'MEDIUM',
  probability: 'MEDIUM',
  owner: '',
  mitigation: '',
  status: 'OPEN',
  dueDate: '',
  linkedTaskIds: [],
})

/** 可选任务（关联风险到具体任务，供选择器使用） */
const taskOptions = computed(() =>
  (store.plan?.taskList ?? []).map((task) => ({ value: task.taskId, label: `${task.taskName}（${task.taskId.slice(0, 8)}）` })),
)

/** 枚举下拉项（取自统一枚举常量，避免模板硬编码） */
const severityOptions = ISSUE_SEVERITY_VALUES.map((value) => ({ value, label: `${value} · ${ISSUE_SEVERITY_LABEL[value]}` }))
const probabilityOptions = ISSUE_PROBABILITY_VALUES.map((value) => ({ value, label: `${value} · ${ISSUE_PROBABILITY_LABEL[value]}` }))
const statusOptions = ISSUE_STATUS_VALUES.map((value) => ({ value, label: ISSUE_STATUS_LABEL[value] }))

async function load(): Promise<void> {
  if (!planId.value) return
  loading.value = true
  errorText.value = ''
  try {
    const result = await api.listRegisters(planId.value)
    items.value = result.items
  } catch (error) {
    items.value = []
    errorText.value = error instanceof ApiClientError ? error.message : String(error)
    ElMessage.error(`加载风险登记册失败：${errorText.value}`)
  } finally {
    loading.value = false
  }
}

function openCreate(): void {
  editingId.value = ''
  form.value = {
    title: '',
    description: '',
    severity: 'MEDIUM',
    probability: 'MEDIUM',
    owner: '',
    mitigation: '',
    status: 'OPEN',
    dueDate: '',
    linkedTaskIds: [],
  }
  dialogVisible.value = true
}

function openEdit(item: RegisterItem): void {
  editingId.value = item.issueId
  form.value = {
    title: item.title,
    description: item.description ?? '',
    severity: item.severity,
    probability: item.probability,
    owner: item.owner ?? '',
    mitigation: item.mitigation ?? '',
    status: item.status,
    dueDate: item.dueDate ?? '',
    linkedTaskIds: [...item.linkedTaskIds],
  }
  dialogVisible.value = true
}

/** 提交表单：空字符串字段不提交，避免写入无意义空值 */
function payloadFromForm(): Partial<RegisterPayload> {
  const payload: Partial<RegisterPayload> = {
    title: form.value.title.trim(),
    severity: form.value.severity,
    probability: form.value.probability,
    status: form.value.status,
    linkedTaskIds: form.value.linkedTaskIds ?? [],
  }
  if (form.value.description?.trim()) payload.description = form.value.description.trim()
  if (form.value.owner?.trim()) payload.owner = form.value.owner.trim()
  if (form.value.mitigation?.trim()) payload.mitigation = form.value.mitigation.trim()
  if (form.value.dueDate) payload.dueDate = form.value.dueDate
  return payload
}

async function submit(): Promise<void> {
  if (!planId.value) return
  if (!form.value.title.trim()) {
    ElMessage.warning('请填写风险标题')
    return
  }
  saving.value = true
  try {
    if (editingId.value) {
      await api.updateRegister(planId.value, editingId.value, payloadFromForm())
      ElMessage.success('风险条目已更新')
    } else {
      await api.createRegister(planId.value, payloadFromForm() as RegisterPayload)
      ElMessage.success('风险已登记')
    }
    dialogVisible.value = false
    await load()
  } catch (error) {
    ElMessage.error(`保存失败：${error instanceof ApiClientError ? error.message : String(error)}`)
  } finally {
    saving.value = false
  }
}

async function remove(item: RegisterItem): Promise<void> {
  if (!planId.value) return
  try {
    await ElMessageBox.confirm(`确认删除风险条目「${item.title}」？`, '删除风险', { type: 'warning' })
  } catch {
    return
  }
  try {
    await api.deleteRegister(planId.value, item.issueId)
    ElMessage.success('风险条目已删除')
    await load()
  } catch (error) {
    ElMessage.error(`删除失败：${error instanceof ApiClientError ? error.message : String(error)}`)
  }
}

/** 生成引擎风险的缓解建议（LLM 不可用时后端自动降级为规则聚合） */
async function advise(): Promise<void> {
  await store.loadAdvice()
}

function exportManual(): void {
  if (items.value.length === 0) {
    ElMessage.warning('暂无人工登记的风险可导出')
    return
  }
  exportCsv(`risk-register-${fileStamp()}.csv`, REGISTER_EXPORT_COLUMNS, items.value)
  ElMessage.success('人工风险登记册已导出')
}

function exportEngine(): void {
  const list = store.plan?.riskList ?? []
  if (list.length === 0) {
    ElMessage.warning('当前计划没有引擎风险可导出')
    return
  }
  exportCsv(`engine-risks-${fileStamp()}.csv`, RISK_EXPORT_COLUMNS, list)
  ElMessage.success('引擎风险清单已导出')
}

/** 任务 ID → 任务名（供关联任务列展示） */
function taskName(taskId: string): string {
  return store.taskIndex.get(taskId)?.taskName ?? taskId
}

onMounted(load)
watch(planId, () => {
  void load()
})
</script>

<template>
  <div class="ip-page">
    <el-empty v-if="!planId" description="请选择或新建一个迭代计划" />

    <template v-else>
      <StatTiles :items="tiles" />

      <div class="ip-panel">
        <div class="ip-panel__title">
          <span>合并风险视图</span>
          <div class="ip-register__actions">
            <el-button size="small" :icon="'Refresh'" @click="load">刷新</el-button>
            <el-button size="small" :icon="'Download'" @click="exportEngine">导出引擎风险</el-button>
            <el-button size="small" :icon="'Download'" @click="exportManual">导出人工登记册</el-button>
            <el-button size="small" type="primary" :icon="'Plus'" @click="openCreate">登记风险</el-button>
          </div>
        </div>

        <div class="ip-register__filters">
          <el-radio-group v-model="filterSource" size="small">
            <el-radio-button value="all">全部</el-radio-button>
            <el-radio-button value="engine">引擎</el-radio-button>
            <el-radio-button value="manual">人工</el-radio-button>
          </el-radio-group>
          <el-select v-model="filterSeverity" size="small" placeholder="全部严重度" clearable style="width: 150px">
            <el-option v-for="item in severityOptions" :key="item.value" :value="item.value" :label="item.label" />
          </el-select>
          <el-select v-model="filterStatus" size="small" placeholder="全部状态" clearable style="width: 150px">
            <el-option v-for="item in statusOptions" :key="item.value" :value="item.value" :label="item.label" />
          </el-select>
          <el-input v-model="keyword" size="small" placeholder="搜索标题 / 描述 / 责任人" clearable style="max-width: 240px" :prefix-icon="'Search'" />
        </div>

        <el-alert v-if="errorText" type="error" :closable="false">{{ errorText }}</el-alert>

        <el-table v-loading="loading" :data="rows" size="small" border>
          <el-table-column label="来源" width="80" align="center">
            <template #default="{ row }">
              <el-tag size="small" effect="plain" :type="row.source === 'engine' ? 'info' : 'primary'">
                {{ row.source === 'engine' ? '引擎' : '人工' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="风险 / 标题" min-width="220">
            <template #default="{ row }">
              <div class="ip-register__title">{{ row.title }}</div>
              <div v-if="row.detail" class="ip-muted ip-register__detail">{{ row.detail }}</div>
            </template>
          </el-table-column>
          <el-table-column label="等级 / 严重度" width="150">
            <template #default="{ row }">
              <el-tag size="small" effect="plain" :type="row.severityTag">{{ row.severityLabel }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="关联任务" min-width="180">
            <template #default="{ row }">
              <span v-if="row.taskIds.length === 0" class="ip-muted">—</span>
              <span v-else class="ip-muted ip-register__tasks">{{ row.taskIds.map(taskName).join(' / ') }}</span>
            </template>
          </el-table-column>
          <el-table-column label="处置" width="140">
            <template #default="{ row }">
              <el-tag v-if="row.statusTag" size="small" effect="plain" :type="row.statusTag">{{ row.statusLabel }}</el-tag>
              <span v-else class="ip-muted">{{ row.owner || '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="更新时间" width="150">
            <template #default="{ row }">
              <span class="ip-mono ip-muted">{{ row.time ? formatIso(row.time) : '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="120" align="center" fixed="right">
            <template #default="{ row }">
              <template v-if="row.issue">
                <el-button size="small" text type="primary" @click="openEdit(row.issue)">编辑</el-button>
                <el-button size="small" text type="danger" @click="remove(row.issue)">删除</el-button>
              </template>
              <span v-else class="ip-muted">只读</span>
            </template>
          </el-table-column>
          <template #empty>
            <el-empty description="暂无风险记录" />
          </template>
        </el-table>
      </div>

      <div class="ip-panel">
        <div class="ip-panel__title">
          <span>缓解建议（Agent）</span>
          <el-button size="small" :loading="store.adviceLoading" :icon="'MagicStick'" @click="advise">生成建议</el-button>
        </div>
        <div v-if="store.advice" class="ip-register__advice">
          <div class="ip-register__advice-head">
            <el-tag size="small" effect="plain" :type="store.advice.mode === 'llm' ? 'primary' : 'info'">
              {{ store.advice.mode === 'llm' ? '模型生成' : '规则聚合（LLM 未配置或调用失败）' }}
            </el-tag>
            <span v-if="store.advice.model" class="ip-muted ip-mono">{{ store.advice.model }}</span>
          </div>
          <p class="ip-register__summary">{{ store.advice.summary }}</p>
          <div v-for="item in store.advice.items" :key="item.riskId" class="ip-register__advice-item">
            <div class="ip-register__advice-title">
              <el-tag size="small" effect="plain" :type="RISK_TAG[item.level] ?? 'info'">{{ RISK_LABEL[item.level] }}</el-tag>
              <span>{{ item.description }}</span>
            </div>
            <ul class="ip-register__actions-list">
              <li v-for="(action, index) in item.actions" :key="index">{{ action }}</li>
            </ul>
          </div>
        </div>
        <el-empty v-else description="尚未生成建议；点击「生成建议」由 Agent 汇总行动项" />
      </div>
    </template>

    <el-dialog v-model="dialogVisible" :title="editingId ? '编辑风险条目' : '登记风险'" width="640px" append-to-body destroy-on-close>
      <el-form label-width="92px" label-position="left">
        <el-form-item label="标题">
          <el-input v-model="form.title" placeholder="一句话描述风险（如：第三方接口不稳定）" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="form.description" type="textarea" :rows="3" placeholder="补充上下文、影响面与触发条件" />
        </el-form-item>
        <el-form-item label="严重度">
          <el-select v-model="form.severity" style="width: 100%">
            <el-option v-for="item in severityOptions" :key="item.value" :value="item.value" :label="item.label" />
          </el-select>
        </el-form-item>
        <el-form-item label="发生概率">
          <el-select v-model="form.probability" style="width: 100%">
            <el-option v-for="item in probabilityOptions" :key="item.value" :value="item.value" :label="item.label" />
          </el-select>
        </el-form-item>
        <el-form-item label="处置状态">
          <el-select v-model="form.status" style="width: 100%">
            <el-option v-for="item in statusOptions" :key="item.value" :value="item.value" :label="item.label" />
          </el-select>
        </el-form-item>
        <el-form-item label="责任人">
          <el-input v-model="form.owner" placeholder="风险跟进人" />
        </el-form-item>
        <el-form-item label="缓解措施">
          <el-input v-model="form.mitigation" type="textarea" :rows="3" placeholder="已采取 / 计划采取的措施" />
        </el-form-item>
        <el-form-item label="处理时限">
          <el-date-picker v-model="form.dueDate" type="date" value-format="YYYY-MM-DD" placeholder="选择日期" style="width: 100%" />
        </el-form-item>
        <el-form-item label="关联任务">
          <el-select v-model="form.linkedTaskIds" multiple filterable placeholder="选择受影响的任务" style="width: 100%">
            <el-option v-for="item in taskOptions" :key="item.value" :value="item.value" :label="item.label" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submit">{{ editingId ? '保存' : '登记' }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.ip-register__actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.ip-register__filters {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.ip-register__title {
  font-weight: 600;
  color: var(--ip-text);
}

.ip-register__detail {
  margin-top: 2px;
  font-size: 12px;
  white-space: normal;
}

.ip-register__tasks {
  font-size: 12px;
}

.ip-register__advice-head {
  display: flex;
  align-items: center;
  gap: 10px;
}

.ip-register__summary {
  margin: 10px 0;
  font-size: 13px;
  color: var(--ip-text);
  line-height: 1.7;
}

.ip-register__advice-item {
  padding: 10px 0;
  border-top: 1px dashed var(--ip-border);
}

.ip-register__advice-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--ip-text);
  flex-wrap: wrap;
}

.ip-register__actions-list {
  margin: 8px 0 0;
  padding-left: 20px;
  font-size: 12.5px;
  color: var(--ip-text-secondary);
  line-height: 1.8;
}
</style>

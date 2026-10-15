<script setup lang="ts">
/**
 * 数据导入对话框：CSV / TSV / JSON（含计划导出 JSON）→ 追加任务或新建计划。
 *
 * 职责边界：
 *  - 前端只做「文本 → 结构化行」的解析与预校验，让错误在提交前就能看到并修正；
 *  - 依赖环路（Kahn 拓扑）**不在前端复刻**：算法唯一事实来源在 TS 引擎，导入逐行落库时
 *    由引擎在标准重规划中判定，命中环则本次导入整体回滚（服务端已实现），此处只把
 *    服务端返回的行级错误呈现出来，并保留用户已粘贴的内容不清空。
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import type { EnumMeta, ImportMode, ImportResult, PlanSummary } from '@/api/types'
import { ApiClientError, api } from '@/api/client'
import { usePlanStore } from '@/stores/plan'
import {
  IMPORT_TEMPLATE_CSV,
  normalizeServerErrors,
  parseImportText,
  rowPreviewText,
  toPayloadRows,
  type ParsedRow,
  type RowIssue,
} from '@/utils/importParse'
import { downloadText } from '@/utils/export'
import { truncate } from '@/utils/format'

const props = withDefaults(
  defineProps<{
    modelValue: boolean
    /** 追加模式的目标计划（默认取当前计划） */
    planId?: string
    /** 可选的目标计划列表（未传则隐藏选择器，直接使用 planId） */
    plans?: PlanSummary[]
    defaultMode?: ImportMode
  }>(),
  { planId: '', plans: () => [], defaultMode: 'append' },
)

const emit = defineEmits<{
  (event: 'update:modelValue', value: boolean): void
  (event: 'imported', result: ImportResult): void
}>()

const store = usePlanStore()

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

const mode = ref<ImportMode>('append')
const targetPlanId = ref('')
const requirement = ref('')
const iterationDeadline = ref('')
const iterationStartDate = ref('')
const devMemberCount = ref(3)
const text = ref('')

const rows = ref<ParsedRow[]>([])
const clientErrors = ref<RowIssue[]>([])
const warnings = ref<string[]>([])
const serverErrors = ref<RowIssue[]>([])
const serverMessage = ref('')
const parsed = ref(false)
const submitting = ref(false)
const textareaRef = ref<HTMLTextAreaElement | null>(null)
/** 聚焦重试定时器（对话框打开动画期间首次 focus 会失效，需要延迟重试一次） */
let focusTimer: number | null = null

/** 合并前端预校验与服务端返回的行级错误（同一行可能同时命中） */
const allErrors = computed<RowIssue[]>(() => [...clientErrors.value, ...serverErrors.value])

/** 行号 → 错误列表 */
const errorMap = computed(() => {
  const map = new Map<number, RowIssue[]>()
  for (const issue of allErrors.value) {
    const list = map.get(issue.row) ?? []
    list.push(issue)
    map.set(issue.row, list)
  }
  return map
})

/** 预览表格数据（未解析时为空） */
const preview = computed(() =>
  rows.value.map((row) => ({
    row: row.row,
    taskName: row.taskName || '（缺失）',
    summary: rowPreviewText(row),
    refs: (row.predecessorRefs ?? []).join(' / '),
    issues: errorMap.value.get(row.row) ?? [],
  })),
)

/** new 模式下的必填项是否齐备 */
const newModeReady = computed(
  () => requirement.value.trim().length > 0 && iterationDeadline.value.length > 0 && devMemberCount.value > 0,
)

/** 追加模式必须选定目标计划 */
const appendReady = computed(() => targetPlanId.value.length > 0)

/** 是否可以提交 */
const canSubmit = computed(
  () =>
    !submitting.value &&
    parsed.value &&
    rows.value.length > 0 &&
    clientErrors.value.length === 0 &&
    (mode.value === 'append' ? appendReady.value : newModeReady.value),
)

/** 枚举元数据尚未加载时先补齐（解析与预校验都需要它） */
async function ensureEnums(): Promise<EnumMeta | null> {
  if (!store.enums) await store.loadEnums()
  return store.enums
}

/** 目标计划既有任务 ID 缓存（前置引用可指向这些已存在任务，避免被误判为无法解析） */
const existingIdsCache = ref<Map<string, Set<string>>>(new Map())

async function existingIdsFor(planId: string): Promise<Set<string>> {
  if (!planId) return new Set()
  if (store.plan?.planId === planId) return new Set(store.plan.taskList.map((task) => task.taskId))
  const cached = existingIdsCache.value.get(planId)
  if (cached) return cached
  try {
    const plan = await api.getPlan(planId)
    const ids = new Set(plan.taskList.map((task) => task.taskId))
    existingIdsCache.value.set(planId, ids)
    return ids
  } catch {
    // 读取失败时退化为「不支持引用既有任务」，只影响预校验提示，不影响服务端最终校验
    return new Set()
  }
}

watch(
  () => props.modelValue,
  async (open) => {
    if (!open) return
    reset()
    mode.value = props.defaultMode
    targetPlanId.value = props.planId || props.plans[0]?.planId || ''
    await ensureEnums()
    await nextTick()
    focusTextarea()
  },
)

watch(mode, () => {
  // 切换模式后：解析结果是基于另一模式的目标计划与既有 taskId 得出的，必须整体作废
  // （保留用户已粘贴的文本，避免重贴）
  rows.value = []
  clientErrors.value = []
  warnings.value = []
  parsed.value = false
  serverErrors.value = []
  serverMessage.value = ''
})

/** 打开后聚焦输入区（打开动画期间 focus 会失效，故重试一次） */
function focusTextarea(): void {
  textareaRef.value?.focus()
  clearFocusTimer()
  focusTimer = window.setTimeout(() => {
    focusTimer = null
    textareaRef.value?.focus()
  }, 80)
}

/** 清理聚焦重试定时器（组件卸载时避免对已销毁的 ref 触发 focus） */
function clearFocusTimer(): void {
  if (focusTimer !== null) {
    window.clearTimeout(focusTimer)
    focusTimer = null
  }
}

onBeforeUnmount(clearFocusTimer)

function reset(): void {
  rows.value = []
  clientErrors.value = []
  warnings.value = []
  serverErrors.value = []
  serverMessage.value = ''
  parsed.value = false
  submitting.value = false
  requirement.value = ''
  iterationDeadline.value = ''
  iterationStartDate.value = ''
  devMemberCount.value = 3
  text.value = ''
  existingIdsCache.value = new Map()
}

/** 解析输入并做前端预校验 */
async function runParse(): Promise<void> {
  const enums = await ensureEnums()
  if (!enums) {
    ElMessage.error('枚举元数据加载失败，无法校验导入数据')
    return
  }
  const existingIds = await existingIdsFor(targetPlanId.value)
  const outcome = parseImportText(text.value, enums, existingIds)
  rows.value = outcome.rows
  clientErrors.value = outcome.errors
  warnings.value = outcome.warnings
  serverErrors.value = []
  serverMessage.value = ''
  parsed.value = true
  if (outcome.rows.length === 0) ElMessage.warning(outcome.warnings.join('；') || '未解析到任何任务行')
  else if (outcome.errors.length === 0) ElMessage.success(`已解析 ${outcome.rows.length} 行，预校验通过`)
  else ElMessage.warning(`已解析 ${outcome.rows.length} 行，发现 ${outcome.errors.length} 处错误`)
}

/** 读取本地文件（csv / json / txt）到文本框 */
function onFilePicked(event: Event): void {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  const reader = new FileReader()
  reader.onload = () => {
    text.value = String(reader.result ?? '')
    void runParse()
  }
  reader.onerror = () => ElMessage.error('文件读取失败，请确认文件编码为 UTF-8')
  reader.readAsText(file, 'utf-8')
  input.value = ''
}

/** 下载导入模板（与导出列对齐，便于往返） */
function downloadTemplate(): void {
  downloadText('task-import-template.csv', `\uFEFF${IMPORT_TEMPLATE_CSV}\r\n`)
  ElMessage.success('导入模板已下载')
}

/** 提交导入 */
async function submit(): Promise<void> {
  if (!canSubmit.value) return
  submitting.value = true
  serverErrors.value = []
  serverMessage.value = ''
  try {
    const result = await api.importTasks({
      mode: mode.value,
      ...(mode.value === 'append'
        ? { planId: targetPlanId.value }
        : {
            requirement: requirement.value.trim(),
            iterationDeadline: iterationDeadline.value,
            iterationStartDate: iterationStartDate.value || iterationDeadline.value,
            devMemberCount: devMemberCount.value,
          }),
      rows: toPayloadRows(rows.value),
    })
    ElMessage.success(
      mode.value === 'append' ? `已导入 ${result.addedCount} 个任务` : `已由导入创建计划：${result.addedCount} 个任务`,
    )
    emit('imported', result)
    visible.value = false
  } catch (error) {
    if (error instanceof ApiClientError) {
      // 传入完整 details 对象 + 信封 message：行级校验失败时它是 {errors, planId, rolledBackPlan}，
      // 逐行落库失败时它是 {row, taskName, ...}；schema 校验失败时它是 FastAPI loc 数组。
      const errors = normalizeServerErrors(error.details, error.message)
      serverErrors.value = errors
      serverMessage.value =
        errors.length > 0 ? `${error.message}（已定位到行级错误，请修正后重试）` : error.message
      ElMessage.error(error.message)
    } else {
      serverMessage.value = String(error)
      ElMessage.error(String(error))
    }
  } finally {
    submitting.value = false
  }
}

/** 仅显示前若干行预览，避免超长表格拖慢页面 */
const PREVIEW_LIMIT = 200
const previewRows = computed(() => preview.value.slice(0, PREVIEW_LIMIT))
const truncatedPreview = computed(() => Math.max(0, preview.value.length - PREVIEW_LIMIT))
</script>

<template>
  <el-dialog v-model="visible" title="数据导入" width="860px" top="6vh" append-to-body destroy-on-close>
    <el-form label-width="96px" label-position="left" class="ip-import__form">
      <el-form-item label="导入方式">
        <el-radio-group v-model="mode">
          <el-radio-button value="append">追加到现有计划</el-radio-button>
          <el-radio-button value="new">由导入行新建计划</el-radio-button>
        </el-radio-group>
      </el-form-item>

      <template v-if="mode === 'append'">
        <el-form-item label="目标计划">
          <el-select v-if="plans.length > 0" v-model="targetPlanId" filterable placeholder="选择目标计划" style="width: 100%">
            <el-option
              v-for="item in plans"
              :key="item.planId"
              :value="item.planId"
              :label="`${truncate(item.rawRequirement, 30)}（${item.taskCount} 任务）`"
            />
          </el-select>
          <el-input v-else :model-value="targetPlanId" disabled placeholder="请先在左侧选择一个计划" />
        </el-form-item>
      </template>

      <template v-else>
        <el-form-item label="需求原文">
          <el-input v-model="requirement" type="textarea" :rows="2" placeholder="用于生成计划的需求描述" />
        </el-form-item>
        <el-form-item label="迭代截止">
          <el-date-picker v-model="iterationDeadline" type="date" value-format="YYYY-MM-DD" placeholder="选择截止日期" style="width: 100%" />
        </el-form-item>
        <el-form-item label="迭代起始">
          <el-date-picker v-model="iterationStartDate" type="date" value-format="YYYY-MM-DD" placeholder="缺省取截止日期" style="width: 100%" />
        </el-form-item>
        <el-form-item label="开发人数">
          <el-input-number v-model="devMemberCount" :min="1" :max="1000" />
        </el-form-item>
      </template>

      <el-form-item label="导入内容">
        <div class="ip-import__source">
          <el-input
            ref="textareaRef"
            v-model="text"
            type="textarea"
            :rows="7"
            placeholder="粘贴 CSV / TSV / JSON，或从下方选择文件。首行应为表头，列名支持「任务名称,任务描述,类型,状态,优先级,责任人,预估工时(h),前置任务」"
          />
          <div class="ip-import__actions">
            <label class="ip-import__file">
              <input type="file" accept=".csv,.tsv,.txt,.json" @change="onFilePicked" />
              <el-button :icon="'UploadFilled'">选择文件</el-button>
            </label>
            <el-button :icon="'Download'" @click="downloadTemplate">下载模板</el-button>
            <el-button type="primary" plain :icon="'MagicStick'" @click="runParse">解析并校验</el-button>
          </div>
        </div>
      </el-form-item>
    </el-form>

    <el-alert v-if="warnings.length > 0" type="warning" :closable="false" class="ip-import__alert">
      <div v-for="(item, index) in warnings" :key="index">{{ item }}</div>
    </el-alert>

    <el-alert v-if="serverMessage" type="error" :closable="false" class="ip-import__alert">
      {{ serverMessage }}
    </el-alert>

    <template v-if="parsed">
      <div class="ip-import__summary">
        <el-tag size="small" effect="plain">解析 {{ rows.length }} 行</el-tag>
        <el-tag size="small" effect="plain" :type="allErrors.length > 0 ? 'danger' : 'success'">
          {{ allErrors.length > 0 ? `${allErrors.length} 处错误` : '校验通过' }}
        </el-tag>
        <span class="ip-muted">导入为逐行写入，任一行失败将整体回滚，不会留下半成品</span>
      </div>

      <el-table :data="previewRows" size="small" max-height="300" border class="ip-import__table">
        <el-table-column label="行" width="56" align="center">
          <template #default="{ row }">{{ row.row }}</template>
        </el-table-column>
        <el-table-column label="任务名称" min-width="180">
          <template #default="{ row }">{{ row.taskName }}</template>
        </el-table-column>
        <el-table-column label="字段预览" min-width="200">
          <template #default="{ row }">
            <span class="ip-muted">{{ row.summary || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="前置引用" min-width="140">
          <template #default="{ row }">
            <span class="ip-muted">{{ row.refs || '—' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="校验" min-width="220">
          <template #default="{ row }">
            <span v-if="row.issues.length === 0" class="ip-import__ok">通过</span>
            <div v-else class="ip-import__issues">
              <el-tag v-for="(issue, index) in row.issues" :key="index" type="danger" size="small" effect="plain">
                {{ issue.field ? `${issue.field}：` : '' }}{{ issue.message }}
              </el-tag>
            </div>
          </template>
        </el-table-column>
      </el-table>
      <div v-if="truncatedPreview > 0" class="ip-muted ip-import__more">
        另有 {{ truncatedPreview }} 行未在预览中展示（提交时会全部导入）
      </div>
    </template>

    <template #footer>
      <div class="ip-import__footer">
        <span class="ip-muted">
          {{ allErrors.length > 0 ? '存在未通过校验的行，修正后重新解析即可提交' : '导入将通过引擎标准重规划校验依赖环' }}
        </span>
        <div>
          <el-button @click="visible = false">取消</el-button>
          <el-button type="primary" :loading="submitting" :disabled="!canSubmit" @click="submit">开始导入</el-button>
        </div>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
.ip-import__form {
  margin-bottom: 4px;
}

.ip-import__source {
  width: 100%;
}

.ip-import__actions {
  display: flex;
  gap: 8px;
  margin-top: 8px;
  flex-wrap: wrap;
}

.ip-import__file input {
  display: none;
}

.ip-import__file {
  display: inline-block;
}

.ip-import__alert {
  margin-top: 8px;
}

.ip-import__summary {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 12px 0 8px;
  font-size: 12.5px;
}

.ip-import__table {
  border: 1px solid var(--ip-border);
  border-radius: var(--ip-radius);
}

.ip-import__issues {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.ip-import__ok {
  color: var(--ip-success);
  font-size: 12.5px;
}

.ip-import__more {
  margin-top: 6px;
  font-size: 12px;
}

.ip-import__footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  font-size: 12.5px;
}
</style>

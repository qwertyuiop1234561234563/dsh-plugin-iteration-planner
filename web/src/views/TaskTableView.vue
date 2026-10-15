<script setup lang="ts">
/**
 * 任务管理页：任务清单增删改查 + 高级筛选 / 保存视图 / 批量编辑 / CSV 导出。
 *
 * 契约说明：
 *  - 任何结构性变更（工时 / 依赖）都会触发**引擎侧**的标准全量重规划；本页只调用既有
 *    单任务接口，绝不在前端复刻任何重规划或依赖校验逻辑；
 *  - 批量编辑以「同一字段、多个任务」为一批**串行**提交，遇循环依赖立即中止，
 *    全部结束后统一刷新一次视图（见 store.batchUpdateTasks）；
 *  - 高级筛选用 `utils/savedViews` 的纯展示条件：保存视图只存**条件**而非任务集合，
 *    因此重规划后依然有效；导出走 `utils/exportPresets` 的统一列定义。
 */
import { computed, nextTick, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { PRIORITY_VALUES, TASK_STATUS_VALUES } from '@/api/types'
import type { ProjectPlan, Task, UpdatableField } from '@/api/types'
import TaskEditDialog from '@/components/TaskEditDialog.vue'
import TaskHistoryDrawer from '@/components/TaskHistoryDrawer.vue'
import { usePlanStore } from '@/stores/plan'
import { detectMilestones } from '@/utils/analytics'
import { exportCsv, fileStamp } from '@/utils/export'
import { taskExportColumns } from '@/utils/exportPresets'
import { PRIORITY_TAG, STATUS_LABEL, STATUS_TAG, TASK_TYPE_LABEL } from '@/utils/format'
import {
  activeFilterCount,
  buildFilterContext,
  emptyFilter,
  filterTasks,
  readSavedViews,
  writeSavedViews,
} from '@/utils/savedViews'
import type { SavedView, TaskFilter } from '@/utils/savedViews'

const store = usePlanStore()
const route = useRoute()
const router = useRouter()

const plan = computed<ProjectPlan | null>(() => store.plan)

/* -------------------------------- 过滤条件 ------------------------------- */

/** 当前筛选条件（初始默认空条件） */
const filter = ref<TaskFilter>(emptyFilter())

/** 高级筛选面板展开态 */
const advancedVisible = ref(false)

/** 里程碑任务集合（只读推断，仅用于筛选与导出标记） */
const milestoneIds = computed(() => new Set(detectMilestones(plan.value).map((item) => item.taskId)))

/** 筛选上下文（关键路径 / 风险命中 / 里程碑 / 孤立节点） */
const filterContext = computed(() => buildFilterContext(plan.value, milestoneIds.value, store.graph?.isolatedTasks))

/** 已启用筛选条件数 */
const filterCount = computed(() => activeFilterCount(filter.value))

/** 过滤后的任务集合 */
const filteredTasks = computed(() => filterTasks(plan.value?.taskList ?? [], filter.value, filterContext.value))

/** 责任人候选（含「待分配」） */
const assigneeOptions = computed(() => {
  const names = new Set<string>()
  for (const task of plan.value?.taskList ?? []) names.add(task.assignee || '待分配')
  return [...names].sort((a, b) => a.localeCompare(b, 'zh-Hans-CN'))
})

const statusOptions = computed(() => store.enums?.taskStatus ?? TASK_STATUS_VALUES)
const typeOptions = computed(() => store.enums?.taskType ?? [])
const priorityOptions = computed(() => store.enums?.priority ?? PRIORITY_VALUES)

function resetFilter(): void {
  filter.value = emptyFilter()
  activeViewId.value = ''
}

/** 前置任务名称串联展示 */
function predecessorText(task: Task): string {
  if (task.predecessorTaskIds.length === 0) return '—'
  return task.predecessorTaskIds.map((id) => store.taskIndex.get(id)?.taskName ?? id.slice(0, 6)).join('、')
}

/* ------------------------------ 保存视图 ------------------------------- */

const savedViews = ref<SavedView[]>(readSavedViews())
const activeViewId = ref('')

function applyView(view: SavedView): void {
  filter.value = { ...emptyFilter(), ...view.filter }
  activeViewId.value = view.id
  advancedVisible.value = true
}

async function saveCurrentView(): Promise<void> {
  if (filterCount.value === 0) {
    ElMessage.warning('当前没有生效的筛选条件，无需保存')
    return
  }
  let name = ''
  try {
    const result = await ElMessageBox.prompt('保存后可在筛选器上一键复用该条件组合', '保存筛选视图', {
      confirmButtonText: '保存',
      cancelButtonText: '取消',
      inputPlaceholder: '例如：P0 未完成任务',
      inputValidator: (value: string) => (value && value.trim().length >= 2 ? true : '视图名称至少 2 个字符'),
    })
    name = result.value.trim()
  } catch {
    return // 用户取消
  }
  const view: SavedView = {
    id: `view-${Date.now().toString(36)}`,
    name,
    filter: { ...filter.value },
  }
  savedViews.value = [view, ...savedViews.value.filter((item) => item.name !== name)]
  writeSavedViews(savedViews.value)
  activeViewId.value = view.id
  ElMessage.success(`筛选视图「${name}」已保存`)
}

async function removeView(view: SavedView): Promise<void> {
  try {
    await ElMessageBox.confirm(`确认删除筛选视图「${view.name}」？`, '删除视图', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return // 用户取消
  }
  savedViews.value = savedViews.value.filter((item) => item.id !== view.id)
  writeSavedViews(savedViews.value)
  if (activeViewId.value === view.id) activeViewId.value = ''
}

/** 下拉选中已保存视图 */
function onViewSelect(id: string): void {
  const view = savedViews.value.find((item) => item.id === id)
  if (view) applyView(view)
}

/** 删除当前激活视图 */
function deleteActiveView(): void {
  const view = savedViews.value.find((item) => item.id === activeViewId.value)
  if (view) void removeView(view)
}

/* ------------------------------- 导出 ------------------------------- */

/** 导出当前过滤结果（CSV，含关键路径 / 里程碑 / 风险标记） */
function exportFiltered(): void {
  const current = plan.value
  if (!current) return
  exportCsv(
    `iteration-tasks-${current.planId}.${fileStamp()}.csv`,
    taskExportColumns({
      criticalIds: filterContext.value.criticalIds,
      milestoneIds: milestoneIds.value,
      riskyIds: filterContext.value.riskyIds,
    }),
    filteredTasks.value,
  )
  ElMessage.success(`已导出 ${filteredTasks.value.length} 条任务`)
}

/* ------------------------------- 批量编辑 ------------------------------ */

const selectedTasks = ref<Task[]>([])
const batchVisible = ref(false)
const batchField = ref<UpdatableField>('status')
/** 文本型批量值（状态 / 优先级 / 责任人） */
const batchText = ref('')
/** 数值型批量值（预估工时） */
const batchHours = ref(8)
const batchSubmitting = ref(false)

/** 批量编辑支持的字段（依赖型字段需逐任务构造，不适合批处理，故不列入） */
const BATCH_FIELDS: Array<{ value: UpdatableField; label: string }> = [
  { value: 'status', label: '任务状态' },
  { value: 'priority', label: '优先级' },
  { value: 'assignee', label: '责任人' },
  { value: 'estimatedHours', label: '预估工时(h)' },
]

function onSelectionChange(rows: Task[]): void {
  selectedTasks.value = rows
}

/** 清空表格选择（批量操作完成后调用） */
function clearSelection(): void {
  tableRef.value?.clearSelection?.()
  selectedTasks.value = []
}

function openBatch(): void {
  batchField.value = 'status'
  batchText.value = store.enums?.taskStatus?.[0] ?? TASK_STATUS_VALUES[0]
  batchHours.value = selectedTasks.value[0]?.estimatedHours ?? 8
  batchVisible.value = true
}

/** 切换批量字段时给出合理的默认值 */
function onBatchFieldChange(field: UpdatableField): void {
  if (field === 'status') batchText.value = store.enums?.taskStatus?.[0] ?? TASK_STATUS_VALUES[0]
  else if (field === 'priority') batchText.value = store.enums?.priority?.[0] ?? PRIORITY_VALUES[0]
  else if (field === 'assignee') batchText.value = selectedTasks.value[0]?.assignee ?? ''
  else batchHours.value = selectedTasks.value[0]?.estimatedHours ?? 8
}

async function submitBatch(): Promise<void> {
  const field = batchField.value
  let value: unknown
  if (field === 'estimatedHours') {
    if (!Number.isFinite(batchHours.value) || batchHours.value <= 0) {
      ElMessage.warning('预估工时需为正数')
      return
    }
    value = batchHours.value
  } else {
    const text = batchText.value.trim()
    if (text === '' && field !== 'assignee') {
      ElMessage.warning('请选择要设置的值')
      return
    }
    value = text
  }

  batchSubmitting.value = true
  try {
    const result = await store.batchUpdateTasks(
      selectedTasks.value.map((task) => task.taskId),
      field,
      value,
    )
    if (result.ok > 0) {
      batchVisible.value = false
      selectedTasks.value = []
      tableRef.value?.clearSelection?.()
    }
  } finally {
    batchSubmitting.value = false
  }
}

/* ------------------------------- 编辑 / 删除 ----------------------------- */

const dialogVisible = ref(false)
const editingTask = ref<Task | null>(null)

function openCreate(): void {
  editingTask.value = null
  dialogVisible.value = true
}

function openEdit(task: Task): void {
  editingTask.value = task
  dialogVisible.value = true
}

async function confirmRemove(task: Task): Promise<void> {
  try {
    await ElMessageBox.confirm(`确认删除任务「${task.taskName}」？其被引用的前置依赖会同步清理。`, '删除确认', {
      type: 'warning',
      confirmButtonText: '删除',
      cancelButtonText: '取消',
    })
  } catch {
    return
  }
  await store.deleteTask(task.taskId)
}

/* ------------------------------- 变更历史 ------------------------------ */

const historyVisible = ref(false)
const historyTask = ref<Task | null>(null)

/** 打开任务变更历史抽屉（数据由服务端从审计重建） */
function openHistory(task: Task): void {
  historyTask.value = task
  historyVisible.value = true
}

/* ------------------------------- 定位高亮 ------------------------------ */

const focusTaskId = ref('')
const tableRef = ref<{ setScrollTop?: (top: number) => void; clearSelection?: () => void } | null>(null)

function scrollToTask(taskId: string): void {
  void nextTick(() => {
    const index = filteredTasks.value.findIndex((task) => task.taskId === taskId)
    if (index >= 0) tableRef.value?.setScrollTop?.(Math.max(0, (index - 3) * 44))
  })
}

/** 支持两种进入方式：`focus=<taskId>`（风险面板跳转）、`keyword=<文本>`（全局搜索 / 日历跳转） */
watch(
  () => [route.query.focus, route.query.keyword],
  ([focus, keyword]) => {
    if (typeof keyword === 'string' && keyword) {
      filter.value = emptyFilter()
      filter.value.keyword = keyword
      activeViewId.value = ''
      focusTaskId.value = ''
      advancedVisible.value = false
      scrollToTask('')
      return
    }
    focusTaskId.value = typeof focus === 'string' ? focus : ''
    if (!focusTaskId.value) return
    // 清除过滤条件，确保目标行可见
    filter.value = emptyFilter()
    activeViewId.value = ''
    scrollToTask(focusTaskId.value)
  },
  { immediate: true },
)

function rowClass({ row }: { row: Task }): string {
  if (row.taskId === focusTaskId.value) return 'ip-row-focus'
  if (row.slack === 0) return 'ip-row-critical'
  return ''
}

function clearFocus(): void {
  focusTaskId.value = ''
  void router.replace({ name: 'plan-tasks', params: { planId: route.params.planId as string } })
}
</script>

<template>
  <div class="ip-page">
    <el-empty v-if="!plan" description="请选择或新建一个迭代计划" />

    <template v-else>
      <div class="ip-panel">
        <div class="ip-panel__title">
          <span>任务清单（{{ filteredTasks.length }} / {{ plan.taskList.length }}）</span>
          <div class="ip-actions">
            <el-button :icon="'Plus'" type="primary" @click="openCreate">新增任务</el-button>
            <el-button :icon="'Download'" @click="exportFiltered">导出 CSV</el-button>
            <el-button :icon="'Refresh'" @click="store.refreshCurrent()">刷新</el-button>
          </div>
        </div>

        <!-- 基础筛选 -->
        <div class="ip-filters">
          <el-input
            v-model="filter.keyword"
            :prefix-icon="'Search'"
            placeholder="按任务名称 / 描述 / 责任人 / ID 搜索"
            clearable
            style="width: 280px"
          />
          <el-select v-model="filter.status" placeholder="全部状态" clearable style="width: 146px">
            <el-option v-for="status in statusOptions" :key="status" :label="STATUS_LABEL[status]" :value="status" />
          </el-select>
          <el-select v-model="filter.taskType" placeholder="全部类型" clearable style="width: 146px">
            <el-option v-for="type in typeOptions" :key="type" :label="TASK_TYPE_LABEL[type]" :value="type" />
          </el-select>
          <el-button :type="advancedVisible ? 'primary' : 'default'" @click="advancedVisible = !advancedVisible">
            高级筛选<template v-if="filterCount > 0">（{{ filterCount }}）</template>
          </el-button>
          <el-button v-if="filterCount > 0" link type="primary" @click="resetFilter">清除全部条件</el-button>

          <el-tag v-if="focusTaskId" closable type="warning" effect="plain" @close="clearFocus">
            已定位：{{ store.taskIndex.get(focusTaskId)?.taskName ?? focusTaskId }}
          </el-tag>
        </div>

        <!-- 高级筛选 -->
        <el-collapse-transition>
          <div v-show="advancedVisible" class="ip-advanced">
            <div class="ip-advanced__row">
              <el-select v-model="filter.priority" placeholder="全部优先级" clearable style="width: 140px">
                <el-option v-for="item in priorityOptions" :key="item" :label="item" :value="item" />
              </el-select>
              <el-select v-model="filter.assignee" placeholder="全部责任人" clearable filterable style="width: 170px">
                <el-option v-for="name in assigneeOptions" :key="name" :label="name" :value="name" />
              </el-select>
              <div class="ip-advanced__hours">
                <span class="ip-muted">工时</span>
                <el-input-number v-model="filter.minHours" :min="0" :max="999" placeholder="最小" controls-position="right" style="width: 122px" />
                <span class="ip-muted">~</span>
                <el-input-number v-model="filter.maxHours" :min="0" :max="999" placeholder="最大" controls-position="right" style="width: 122px" />
              </div>
            </div>
            <div class="ip-advanced__row">
              <el-checkbox v-model="filter.onlyCritical">仅看关键路径</el-checkbox>
              <el-checkbox v-model="filter.onlyRisky">仅看命中风险</el-checkbox>
              <el-checkbox v-model="filter.onlyMilestone">仅看里程碑</el-checkbox>
              <el-checkbox v-model="filter.onlyIsolated">仅看孤立任务</el-checkbox>
            </div>
            <div class="ip-advanced__row ip-advanced__views">
              <span class="ip-muted">保存视图</span>
              <el-select
                :model-value="activeViewId"
                placeholder="选择已保存视图"
                clearable
                style="width: 220px"
                @change="onViewSelect"
              >
                <el-option v-for="view in savedViews" :key="view.id" :label="view.name" :value="view.id" />
              </el-select>
              <el-button size="small" :icon="'Star'" @click="saveCurrentView">保存当前视图</el-button>
              <el-button v-if="activeViewId" size="small" type="danger" plain :icon="'Delete'" @click="deleteActiveView">
                删除视图
              </el-button>
            </div>
          </div>
        </el-collapse-transition>

        <!-- 批量操作条 -->
        <div v-if="selectedTasks.length > 0" class="ip-batch-bar">
          <span>已选择 <strong>{{ selectedTasks.length }}</strong> 个任务</span>
          <el-button type="primary" size="small" :icon="'EditPen'" @click="openBatch">批量编辑</el-button>
          <el-button size="small" @click="clearSelection">清除选择</el-button>
        </div>

        <el-table
          ref="tableRef"
          v-loading="store.detailLoading"
          :data="filteredTasks"
          :row-class-name="rowClass"
          row-key="taskId"
          stripe
          height="600"
          empty-text="暂无匹配任务"
          @selection-change="onSelectionChange"
        >
          <el-table-column type="selection" width="46" />
          <el-table-column type="index" label="#" width="52" />
          <el-table-column label="任务名称" min-width="220">
            <template #default="{ row }">
              <div class="ip-task-name">
                <span>{{ row.taskName }}</span>
                <el-tag v-if="row.slack === 0" size="small" type="danger" effect="plain">关键</el-tag>
                <el-tag v-else-if="store.graph?.isolatedTasks.includes(row.taskId)" size="small" type="info" effect="plain">
                  孤立
                </el-tag>
                <el-tag v-if="milestoneIds.has(row.taskId)" size="small" type="warning" effect="plain">里程碑</el-tag>
              </div>
              <div class="ip-mono ip-muted">{{ row.taskId }}</div>
            </template>
          </el-table-column>
          <el-table-column label="类型" width="112">
            <template #default="{ row }">
              <el-tag size="small" effect="plain">{{ TASK_TYPE_LABEL[row.taskType as keyof typeof TASK_TYPE_LABEL] }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="110">
            <template #default="{ row }">
              <el-tag size="small" :type="STATUS_TAG[row.status as keyof typeof STATUS_TAG]">
                {{ STATUS_LABEL[row.status as keyof typeof STATUS_LABEL] }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="优先级" width="92" align="center">
            <template #default="{ row }">
              <el-tag size="small" :type="PRIORITY_TAG[row.priority as keyof typeof PRIORITY_TAG]" effect="plain">
                {{ row.priority }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="工时" width="80" align="center">
            <template #default="{ row }">{{ row.estimatedHours }}h</template>
          </el-table-column>
          <el-table-column label="前序依赖" min-width="190">
            <template #default="{ row }"><span class="ip-muted">{{ predecessorText(row) }}</span></template>
          </el-table-column>
          <el-table-column label="排期（es / ls / slack）" width="176" align="center">
            <template #default="{ row }">
              <span class="ip-mono">{{ row.es }} / {{ row.ls }} / {{ row.slack }}</span>
            </template>
          </el-table-column>
          <el-table-column label="责任人" width="110">
            <template #default="{ row }">{{ row.assignee || '—' }}</template>
          </el-table-column>
          <el-table-column label="操作" width="176" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="openEdit(row)">编辑</el-button>
              <el-button link size="small" @click="openHistory(row)">历史</el-button>
              <el-button link type="danger" size="small" @click="confirmRemove(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <TaskEditDialog v-model="dialogVisible" :plan="plan" :task="editingTask" />
      <TaskHistoryDrawer
        v-model="historyVisible"
        :plan-id="plan.planId"
        :task-id="historyTask?.taskId ?? ''"
        :task-name="historyTask?.taskName"
      />

      <!-- 批量编辑 -->
      <el-dialog v-model="batchVisible" title="批量编辑任务" width="460px">
        <p class="ip-muted" style="margin-top: 0">
          将对已选中的 {{ selectedTasks.length }} 个任务提交「同一字段」修改；引擎会在每次更新后执行其标准全量重规划。
        </p>
        <el-form label-width="92px">
          <el-form-item label="编辑字段">
            <el-select v-model="batchField" style="width: 100%" @change="onBatchFieldChange">
              <el-option v-for="field in BATCH_FIELDS" :key="field.value" :label="field.label" :value="field.value" />
            </el-select>
          </el-form-item>
          <el-form-item label="设置为">
            <el-select v-if="batchField === 'status'" v-model="batchText" style="width: 100%">
              <el-option v-for="status in statusOptions" :key="status" :label="STATUS_LABEL[status]" :value="status" />
            </el-select>
            <el-select v-else-if="batchField === 'priority'" v-model="batchText" style="width: 100%">
              <el-option v-for="item in priorityOptions" :key="item" :label="item" :value="item" />
            </el-select>
            <el-input v-else-if="batchField === 'assignee'" v-model="batchText" placeholder="填写责任人姓名" clearable />
            <el-input-number
              v-else
              v-model="batchHours"
              :min="0.5"
              :max="999"
              :step="1"
              controls-position="right"
              style="width: 100%"
            />
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="batchVisible = false">取消</el-button>
          <el-button type="primary" :loading="batchSubmitting" @click="submitBatch">
            提交批量更新
          </el-button>
        </template>
      </el-dialog>
    </template>
  </div>
</template>

<style scoped>
.ip-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.ip-filters {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 14px;
  flex-wrap: wrap;
}

.ip-advanced {
  display: grid;
  gap: 10px;
  padding: 12px 14px;
  margin-bottom: 14px;
  border: 1px dashed var(--ip-border);
  border-radius: var(--ip-radius);
  background: var(--ip-surface-sunk);
}

.ip-advanced__row {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
}

.ip-advanced__hours {
  display: flex;
  align-items: center;
  gap: 8px;
}

.ip-advanced__views {
  padding-top: 8px;
  border-top: 1px dashed var(--ip-border);
}

.ip-batch-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 12px;
  margin-bottom: 12px;
  border: 1px solid var(--ip-brand);
  border-radius: var(--ip-radius);
  background: color-mix(in srgb, var(--ip-brand) 8%, transparent);
  font-size: 13px;
}

.ip-task-name {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
}

:deep(.ip-row-focus) {
  background: color-mix(in srgb, var(--ip-signal) 16%, transparent) !important;
}

:deep(.ip-row-critical td:first-child) {
  box-shadow: inset 3px 0 0 var(--ip-danger);
}
</style>

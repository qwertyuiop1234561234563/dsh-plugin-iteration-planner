<script setup lang="ts">
/**
 * 任务管理页：任务清单增删改查，任意结构性变更（工时 / 依赖）都会触发后端重规划。
 *
 * 支持按关键字、状态、关键路径过滤；从风险面板跳转时可通过 `focus` 查询参数高亮目标任务。
 */
import { computed, nextTick, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import type { ProjectPlan, Task, TaskStatus } from '@/api/types'
import TaskEditDialog from '@/components/TaskEditDialog.vue'
import { usePlanStore } from '@/stores/plan'
import { PRIORITY_TAG, STATUS_LABEL, STATUS_TAG, TASK_TYPE_LABEL } from '@/utils/format'

const store = usePlanStore()
const route = useRoute()
const router = useRouter()

const plan = computed<ProjectPlan | null>(() => store.plan)

/* -------------------------------- 过滤条件 ------------------------------- */

const keyword = ref('')
const statusFilter = ref<TaskStatus | ''>('')
const criticalOnly = ref(false)

const filteredTasks = computed(() => {
  const current = plan.value
  if (!current) return []
  const lowered = keyword.value.trim().toLowerCase()
  return current.taskList.filter((task) => {
    if (statusFilter.value && task.status !== statusFilter.value) return false
    if (criticalOnly.value && task.slack !== 0) return false
    if (!lowered) return true
    return (
      task.taskName.toLowerCase().includes(lowered) ||
      task.taskId.toLowerCase().includes(lowered) ||
      (task.assignee ?? '').toLowerCase().includes(lowered)
    )
  })
})

/** 前置任务名称串联展示 */
function predecessorText(task: Task): string {
  if (task.predecessorTaskIds.length === 0) return '—'
  return task.predecessorTaskIds.map((id) => store.taskIndex.get(id)?.taskName ?? id.slice(0, 6)).join('、')
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

/* ------------------------------- 风险定位高亮 ---------------------------- */

const focusTaskId = ref('')
const tableRef = ref<{ setScrollTop?: (top: number) => void } | null>(null)

watch(
  () => route.query.focus,
  (focus) => {
    focusTaskId.value = typeof focus === 'string' ? focus : ''
    if (!focusTaskId.value) return
    // 清除过滤条件，确保目标行可见
    keyword.value = ''
    statusFilter.value = ''
    criticalOnly.value = false
    void nextTick(() => {
      const index = filteredTasks.value.findIndex((task) => task.taskId === focusTaskId.value)
      if (index >= 0) tableRef.value?.setScrollTop?.(Math.max(0, (index - 3) * 44))
    })
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

const statusOptions = computed(() => store.enums?.taskStatus ?? [])
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
            <el-button :icon="'Refresh'" @click="store.refreshCurrent()">刷新</el-button>
          </div>
        </div>

        <div class="ip-filters">
          <el-input
            v-model="keyword"
            :prefix-icon="'Search'"
            placeholder="按任务名称 / 状态 / 责任人搜索"
            clearable
            style="width: 260px"
          />
          <el-select v-model="statusFilter" placeholder="全部状态" clearable style="width: 150px">
            <el-option v-for="status in statusOptions" :key="status" :label="STATUS_LABEL[status]" :value="status" />
          </el-select>
          <el-checkbox v-model="criticalOnly">仅看关键路径</el-checkbox>
          <el-tag v-if="focusTaskId" closable type="warning" effect="plain" @close="clearFocus">
            已定位：{{ store.taskIndex.get(focusTaskId)?.taskName ?? focusTaskId }}
          </el-tag>
        </div>

        <el-table
          ref="tableRef"
          v-loading="store.detailLoading"
          :data="filteredTasks"
          :row-class-name="rowClass"
          stripe
          height="620"
          empty-text="暂无任务"
        >
          <el-table-column type="index" label="#" width="52" />
          <el-table-column label="任务名称" min-width="220">
            <template #default="{ row }">
              <div class="ip-task-name">
                <span>{{ row.taskName }}</span>
                <el-tag v-if="row.slack === 0" size="small" type="danger" effect="plain">关键</el-tag>
                <el-tag v-else-if="store.graph?.isolatedTasks.includes(row.taskId)" size="small" type="info" effect="plain">
                  孤立
                </el-tag>
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
          <el-table-column label="操作" width="130" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" size="small" @click="openEdit(row)">编辑</el-button>
              <el-button link type="danger" size="small" @click="confirmRemove(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <TaskEditDialog v-model="dialogVisible" :plan="plan" :task="editingTask" />
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

.ip-task-name {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
}

:deep(.ip-row-focus) {
  background: #fff7e6 !important;
}

:deep(.ip-row-critical td:first-child) {
  box-shadow: inset 3px 0 0 var(--ip-danger);
}
</style>
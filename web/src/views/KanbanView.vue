<script setup lang="ts">
/**
 * 任务看板（Kanban）：按状态分列，支持原生 HTML5 拖拽改状态。
 *
 * 关键行为：拖拽落列即调用 `PATCH /plans/:planId/tasks/:taskId`（updateField=status），
 * 由引擎按固定顺序重跑「DAG 校验 → AOE 排期 → 风险扫描 → 燃尽」，前端随后整表刷新，
 * 保证看板与排期口径一致（避免前端“假状态”与引擎结果漂移）。
 */
import { computed, ref } from 'vue'
import type { ProjectPlan, Task, TaskStatus } from '@/api/types'
import { TASK_STATUS_VALUES } from '@/api/types'
import { usePlanStore } from '@/stores/plan'
import { PRIORITY_TAG, STATUS_LABEL, STATUS_TAG, TASK_TYPE_LABEL } from '@/utils/format'

const store = usePlanStore()
const plan = computed<ProjectPlan | null>(() => store.plan)

/** 列定义（顺序取引擎枚举元数据，缺失时回退到内置常量） */
const columns = computed<TaskStatus[]>(() => store.enums?.taskStatus ?? [...TASK_STATUS_VALUES])

const keyword = ref('')
const draggingId = ref('')
const dragOverStatus = ref('')

/** 过滤后的任务（关键字匹配名称 / 责任人 / 类型） */
const visibleTasks = computed(() => {
  const lowered = keyword.value.trim().toLowerCase()
  const list = plan.value?.taskList ?? []
  if (!lowered) return list
  return list.filter(
    (task) =>
      task.taskName.toLowerCase().includes(lowered) ||
      (task.assignee ?? '').toLowerCase().includes(lowered) ||
      TASK_TYPE_LABEL[task.taskType].includes(lowered) ||
      task.taskId.toLowerCase().includes(lowered),
  )
})

/** 按状态分组（列顺序稳定） */
const grouped = computed(() => {
  const map = new Map<TaskStatus, Task[]>()
  for (const status of columns.value) map.set(status, [])
  for (const task of visibleTasks.value) {
    const bucket = map.get(task.status) ?? []
    bucket.push(task)
    map.set(task.status, bucket)
  }
  return map
})

/** 某列的任务按「关键路径优先 + es 升序」排序，突出紧急项 */
function tasksOf(status: TaskStatus): Task[] {
  const list = [...(grouped.value.get(status) ?? [])]
  return list.sort((a, b) => a.slack - b.slack || a.es - b.es)
}

/* ------------------------------- 拖拽交互 ------------------------------- */

function onDragStart(taskId: string, event: DragEvent): void {
  draggingId.value = taskId
  event.dataTransfer?.setData('text/plain', taskId)
  if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move'
}

function onDragEnd(): void {
  draggingId.value = ''
  dragOverStatus.value = ''
}

/** 落下：仅当状态确实改变时才触发重规划 */
async function onDrop(status: TaskStatus): Promise<void> {
  const taskId = draggingId.value
  dragOverStatus.value = ''
  draggingId.value = ''
  if (!taskId) return
  const task = store.taskIndex.get(taskId)
  if (!task || task.status === status) return
  await store.updateTask(taskId, { updateField: 'status', newValue: status })
}

/** 看板只统计引擎枚举中的合法状态，避免未知状态丢列 */
const total = computed(() => visibleTasks.value.length)
</script>

<template>
  <div class="ip-page">
    <el-empty v-if="!plan" description="请选择或新建一个迭代计划" />

    <template v-else>
      <div class="ip-panel">
        <div class="ip-panel__title">
          <span>任务看板</span>
          <div class="ip-kanban-actions">
            <el-input
              v-model="keyword"
              :prefix-icon="'Search'"
              placeholder="按任务 / 责任人 / 类型过滤"
              clearable
              style="width: 240px"
            />
            <el-button :icon="'Refresh'" @click="store.refreshCurrent()">刷新</el-button>
          </div>
        </div>
        <el-alert
          type="info"
          :closable="false"
          show-icon
          title="拖动卡片到其它列即可更新任务状态；状态变更会触发引擎全量重规划（排期 / 风险 / 燃尽同步刷新）。"
        />
      </div>

      <div class="ip-kanban" v-loading="store.mutating">
        <div
          v-for="status in columns"
          :key="status"
          class="ip-kanban__column"
          :class="{ 'is-over': dragOverStatus === status }"
          @dragover.prevent="dragOverStatus = status"
          @dragleave="dragOverStatus === status && (dragOverStatus = '')"
          @drop.prevent="onDrop(status)"
        >
          <div class="ip-kanban__head">
            <el-tag size="small" :type="STATUS_TAG[status]" effect="plain">{{ STATUS_LABEL[status] }}</el-tag>
            <span class="ip-muted">{{ tasksOf(status).length }}</span>
          </div>

          <div class="ip-kanban__list ip-scroll">
            <el-empty v-if="tasksOf(status).length === 0" description="暂无任务" :image-size="52" />
            <div
              v-for="task in tasksOf(status)"
              :key="task.taskId"
              class="ip-kanban__card"
              :class="{ 'is-critical': task.slack === 0, 'is-dragging': draggingId === task.taskId }"
              draggable="true"
              @dragstart="onDragStart(task.taskId, $event)"
              @dragend="onDragEnd"
            >
              <div class="ip-kanban__card-title">
                <span class="ip-kanban__card-name" :title="task.taskName">{{ task.taskName }}</span>
                <el-tag v-if="task.slack === 0" size="small" type="danger" effect="plain">关键</el-tag>
              </div>
              <div class="ip-kanban__card-tags">
                <el-tag size="small" effect="plain" type="info">{{ TASK_TYPE_LABEL[task.taskType] }}</el-tag>
                <el-tag size="small" effect="plain" :type="PRIORITY_TAG[task.priority]">{{ task.priority }}</el-tag>
              </div>
              <div class="ip-kanban__card-meta ip-muted">
                <span>{{ task.estimatedHours }}h</span>
                <span>es {{ task.es }} / slack {{ task.slack }}</span>
              </div>
              <div class="ip-kanban__card-foot">
                <span class="ip-kanban__assignee">{{ task.assignee || '待分配' }}</span>
                <span class="ip-mono ip-muted">{{ task.taskId.slice(0, 6) }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div class="ip-muted" style="font-size: 12px">当前视图共 {{ total }} / {{ plan.taskList.length }} 个任务</div>
    </template>
  </div>
</template>

<style scoped>
.ip-kanban-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.ip-kanban {
  display: flex;
  gap: 12px;
  overflow-x: auto;
  padding-bottom: 6px;
  align-items: flex-start;
}

.ip-kanban__column {
  flex: 0 0 258px;
  background: var(--ip-surface-sunk);
  border: 1px solid var(--ip-border);
  border-radius: var(--ip-radius);
  padding: 10px;
  transition: background 0.15s, border-color 0.15s;
}

.ip-kanban__column.is-over {
  background: rgba(36, 104, 242, 0.08);
  border-color: var(--ip-brand);
}

.ip-kanban__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}

.ip-kanban__list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 560px;
  min-height: 80px;
}

.ip-kanban__card {
  background: var(--ip-surface);
  border: 1px solid var(--ip-border);
  border-radius: 8px;
  padding: 10px 12px;
  box-shadow: 0 1px 3px rgba(31, 45, 61, 0.06);
  cursor: grab;
  transition: box-shadow 0.15s, transform 0.15s;
}

.ip-kanban__card:hover {
  box-shadow: 0 4px 12px rgba(31, 45, 61, 0.12);
  transform: translateY(-1px);
}

.ip-kanban__card.is-dragging {
  opacity: 0.45;
  cursor: grabbing;
}

.ip-kanban__card.is-critical {
  box-shadow: inset 3px 0 0 var(--ip-danger), 0 1px 3px rgba(31, 45, 61, 0.06);
}

.ip-kanban__card-title {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
}

.ip-kanban__card-name {
  font-size: 13px;
  font-weight: 600;
  flex: 1;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.ip-kanban__card-tags {
  display: flex;
  gap: 6px;
  margin-bottom: 6px;
  flex-wrap: wrap;
}

.ip-kanban__card-meta {
  display: flex;
  justify-content: space-between;
  font-size: 11.5px;
  margin-bottom: 6px;
}

.ip-kanban__card-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 11.5px;
}

.ip-kanban__assignee {
  color: var(--ip-text-secondary);
}
</style>
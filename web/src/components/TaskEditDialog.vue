<script setup lang="ts">
/**
 * 任务编辑弹窗：新增任务 / 修改任务可更新字段（工时、依赖、状态、优先级、责任人）。
 *
 * 与后端契约对齐（§四.5）：修改走「单字段更新」接口，每个变更字段一次调用，
 * 每次调用后端都会重跑环检测与 AOE 排期；此处按顺序提交并逐次校验，
 * 一旦某个字段更新失败（例如引入循环依赖返回 409）立即中止，避免部分成功难以追踪。
 */
import { computed, reactive, watch } from 'vue'
import { ElMessage } from 'element-plus'
import type { Priority, ProjectPlan, Task, TaskStatus, TaskType, UpdatableField } from '@/api/types'
import { usePlanStore } from '@/stores/plan'

const visible = defineModel<boolean>({ required: true })

const props = defineProps<{
  plan: ProjectPlan
  /** 为空表示新增模式 */
  task: Task | null
}>()

const store = usePlanStore()

const isEdit = computed(() => props.task !== null)

const form = reactive({
  taskName: '',
  taskDesc: '',
  taskType: 'BUSINESS_CODE' as TaskType,
  estimatedHours: 8,
  predecessorTaskIds: [] as string[],
  status: 'TODO' as TaskStatus,
  priority: 'P2' as Priority,
  assignee: '',
})

/** 可选前置任务（排除自身，防止自依赖） */
const predecessorOptions = computed(() =>
  props.plan.taskList
    .filter((item) => item.taskId !== props.task?.taskId)
    .map((item) => ({ value: item.taskId, label: `${item.taskName}（${item.taskId.slice(0, 6)}）` })),
)

watch(visible, (open) => {
  if (open) reset()
})
watch(
  () => props.task,
  () => {
    if (visible.value) reset()
  },
)

function reset(): void {
  const task = props.task
  if (task) {
    form.taskName = task.taskName
    form.taskDesc = task.taskDesc
    form.taskType = task.taskType
    form.estimatedHours = task.estimatedHours
    form.predecessorTaskIds = [...task.predecessorTaskIds]
    form.status = task.status
    form.priority = task.priority
    form.assignee = task.assignee ?? ''
    return
  }
  form.taskName = ''
  form.taskDesc = ''
  form.taskType = 'BUSINESS_CODE'
  form.estimatedHours = 8
  form.predecessorTaskIds = []
  form.status = 'TODO'
  form.priority = 'P2'
  form.assignee = ''
}

/** 比较依赖 ID 集合是否等价（顺序无关） */
function sameIds(a: readonly string[], b: readonly string[]): boolean {
  if (a.length !== b.length) return false
  const set = new Set(b)
  return a.every((id) => set.has(id))
}

/** 收集与原任务的差异字段 */
function collectChanges(task: Task): Array<{ field: UpdatableField; value: unknown }> {
  const changes: Array<{ field: UpdatableField; value: unknown }> = []
  if (form.estimatedHours !== task.estimatedHours) changes.push({ field: 'estimatedHours', value: form.estimatedHours })
  if (!sameIds(form.predecessorTaskIds, task.predecessorTaskIds)) {
    changes.push({ field: 'predecessorTaskIds', value: form.predecessorTaskIds })
  }
  if (form.status !== task.status) changes.push({ field: 'status', value: form.status })
  if (form.priority !== task.priority) changes.push({ field: 'priority', value: form.priority })
  if (form.assignee !== (task.assignee ?? '')) changes.push({ field: 'assignee', value: form.assignee })
  return changes
}

async function submit(): Promise<void> {
  if (isEdit.value && props.task) {
    const changes = collectChanges(props.task)
    if (changes.length === 0) {
      visible.value = false
      return
    }
    for (const change of changes) {
      const ok = await store.updateTask(props.task.taskId, {
        updateField: change.field,
        newValue: change.value,
      })
      if (!ok) return // 失败已由 store 提示（含循环依赖专项文案），中止后续变更
    }
    visible.value = false
    return
  }

  if (form.taskName.trim().length === 0) {
    ElMessage.warning('请填写任务名称')
    return
  }
  const ok = await store.addTask({
    taskName: form.taskName.trim(),
    taskDesc: form.taskDesc,
    taskType: form.taskType,
    estimatedHours: form.estimatedHours,
    predecessorTaskIds: form.predecessorTaskIds,
    status: form.status,
    priority: form.priority,
    assignee: form.assignee.trim() || undefined,
  })
  if (ok) visible.value = false
}

/** 从枚举元数据读取下拉项（后端为单一事实来源） */
const typeOptions = computed(() => store.enums?.taskType ?? [])
const statusOptions = computed(() => store.enums?.taskStatus ?? [])
const priorityOptions = computed(() => store.enums?.priority ?? [])
</script>

<template>
  <el-dialog v-model="visible" :title="isEdit ? '编辑任务' : '新增任务'" width="640px" top="6vh">
    <el-alert
      v-if="isEdit"
      type="warning"
      :closable="false"
      show-icon
      title="保存后会立即重跑环检测与关键路径排期；若引入循环依赖，本次修改将被拒绝。"
      style="margin-bottom: 14px"
    />
    <el-form label-width="110px">
      <el-form-item label="任务名称">
        <el-input v-model="form.taskName" :disabled="isEdit" placeholder="例如：订单创建接口开发" maxlength="80" />
      </el-form-item>
      <el-form-item label="任务描述">
        <el-input
          v-model="form.taskDesc"
          type="textarea"
          :rows="2"
          :disabled="isEdit"
          placeholder="任务范围与验收要点"
          maxlength="400"
        />
      </el-form-item>
      <el-form-item label="任务类型">
        <el-select v-model="form.taskType" :disabled="isEdit" style="width: 100%">
          <el-option v-for="type in typeOptions" :key="type" :label="type" :value="type" />
        </el-select>
      </el-form-item>
      <el-form-item label="预估工时(h)">
        <el-input-number v-model="form.estimatedHours" :min="0.5" :max="1000" :step="0.5" />
        <span class="ip-muted" style="margin-left: 10px">重规划时按缓冲规则上浮</span>
      </el-form-item>
      <el-form-item label="前置任务">
        <el-select
          v-model="form.predecessorTaskIds"
          multiple
          filterable
          collapse-tags
          collapse-tags-tooltip
          placeholder="选择必须先行完成的任务"
          style="width: 100%"
        >
          <el-option v-for="item in predecessorOptions" :key="item.value" :label="item.label" :value="item.value" />
        </el-select>
      </el-form-item>
      <el-form-item label="状态">
        <el-select v-model="form.status" style="width: 100%">
          <el-option v-for="status in statusOptions" :key="status" :label="status" :value="status" />
        </el-select>
      </el-form-item>
      <el-form-item label="优先级">
        <el-select v-model="form.priority" style="width: 100%">
          <el-option v-for="priority in priorityOptions" :key="priority" :label="priority" :value="priority" />
        </el-select>
      </el-form-item>
      <el-form-item label="责任人">
        <el-input v-model="form.assignee" placeholder="可选，填写姓名或账号" maxlength="40" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="store.mutating" @click="submit">
        {{ isEdit ? '保存并重规划' : '新增并重规划' }}
      </el-button>
    </template>
  </el-dialog>
</template>
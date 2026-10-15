<script setup lang="ts">
/**
 * 任务变更历史抽屉：按 taskId 过滤审计记录，还原字段级变更时间线。
 *
 * 数据来源为后端由审计重建的结果（`GET /api/workspace/plans/:planId/tasks/:taskId/history`），
 * 前端只做展示与数值格式化，不推断任何业务结论。
 */
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { ApiClientError, api } from '@/api/client'
import type { TaskHistoryEntry, TaskHistoryResult } from '@/api/types'
import { STATUS_LABEL, UPDATABLE_FIELD_LABEL, auditActionLabel, formatIso } from '@/utils/format'
import type { UpdatableField } from '@/api/types'

const props = defineProps<{
  modelValue: boolean
  planId: string
  taskId: string
  taskName?: string
}>()

const emit = defineEmits<{ (event: 'update:modelValue', value: boolean): void }>()

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

const result = ref<TaskHistoryResult | null>(null)
const loading = ref(false)
const errorText = ref('')

/** 元素取值 → 可读文案（枚举字段用统一映射，其余原样展示） */
function displayValue(field: string | null, value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  if (field === 'status') return STATUS_LABEL[value as keyof typeof STATUS_LABEL] ?? String(value)
  if (field === 'estimatedHours') return `${value} h`
  if (field === 'predecessorTaskIds') {
    return Array.isArray(value) && value.length > 0 ? value.join(' / ') : '（无）'
  }
  return String(value)
}

/** 字段文案 */
function fieldLabel(field: string | null): string {
  if (!field) return '任务'
  return UPDATABLE_FIELD_LABEL[field as UpdatableField] ?? field
}

/** 时间线条目类型（沿用 Element Plus 时间线语义颜色） */
function timelineType(entry: TaskHistoryEntry): 'primary' | 'success' | 'warning' | 'danger' | 'info' {
  if (!entry.ok) return 'danger'
  if (entry.action === 'task_add') return 'success'
  if (entry.action === 'task_delete') return 'warning'
  return 'primary'
}

async function load(): Promise<void> {
  if (!props.planId || !props.taskId) return
  loading.value = true
  errorText.value = ''
  try {
    result.value = await api.taskHistory(props.planId, props.taskId)
  } catch (error) {
    result.value = null
    errorText.value = error instanceof ApiClientError ? error.message : String(error)
    ElMessage.error(`加载变更历史失败：${errorText.value}`)
  } finally {
    loading.value = false
  }
}

watch(
  () => [props.modelValue, props.taskId] as const,
  ([open]) => {
    if (open) void load()
  },
)
</script>

<template>
  <el-drawer v-model="visible" :title="`变更历史 · ${taskName ?? taskId}`" size="440px" destroy-on-close>
    <div v-loading="loading" class="ip-history">
      <el-alert v-if="errorText" type="error" :closable="false">{{ errorText }}</el-alert>

      <template v-else-if="result">
        <div class="ip-history__meta">
          <el-tag size="small" effect="plain">{{ result.entryCount }} 条记录</el-tag>
          <el-tag size="small" effect="plain" type="info">taskId：{{ result.taskId }}</el-tag>
          <el-button size="small" :icon="'Refresh'" @click="load">刷新</el-button>
        </div>

        <el-empty v-if="result.entries.length === 0" description="暂无变更记录（任务创建后未发生字段变更）" />

        <el-timeline v-else>
          <el-timeline-item
            v-for="(entry, index) in result.entries"
            :key="entry.recordId ?? index"
            :timestamp="formatIso(entry.ts)"
            :type="timelineType(entry)"
            placement="top"
          >
            <div class="ip-history__title">
              <strong>{{ auditActionLabel(entry.action) }}</strong>
              <el-tag v-if="!entry.ok" type="danger" size="small" effect="plain">失败</el-tag>
              <el-tag v-else-if="entry.action === 'task_update'" size="small" effect="plain">{{ fieldLabel(entry.field) }}</el-tag>
            </div>
            <div v-if="entry.action === 'task_update'" class="ip-history__diff">
              <span class="ip-history__from">{{ displayValue(entry.field, entry.from) }}</span>
              <span class="ip-history__arrow">→</span>
              <span class="ip-history__to">{{ displayValue(entry.field, entry.to) }}</span>
            </div>
            <div v-else-if="entry.action === 'task_add'" class="ip-history__diff">
              <span class="ip-history__to">{{ displayValue('taskName', entry.to) }}</span>
            </div>
            <div v-else-if="entry.action === 'task_delete'" class="ip-history__diff">
              <span class="ip-history__from">已删除</span>
            </div>
            <div v-if="entry.error" class="ip-history__error">{{ entry.error }}</div>
          </el-timeline-item>
        </el-timeline>
      </template>
    </div>
  </el-drawer>
</template>

<style scoped>
.ip-history {
  min-height: 120px;
}

.ip-history__meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}

.ip-history__title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--ip-text);
}

.ip-history__diff {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 6px;
  font-size: 12.5px;
  font-family: var(--ip-font-mono);
  flex-wrap: wrap;
}

.ip-history__from {
  color: var(--ip-text-muted);
  text-decoration: line-through;
}

.ip-history__arrow {
  color: var(--ip-text-muted);
}

.ip-history__to {
  color: var(--ip-text);
  font-weight: 600;
}

.ip-history__error {
  margin-top: 6px;
  font-size: 12px;
  color: var(--ip-danger);
}
</style>

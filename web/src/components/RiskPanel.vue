<script setup lang="ts">
/**
 * 风险面板：按等级展示风险条目、关联任务与整改建议。
 *
 * 高风险项使用醒目左侧色条与红色标签，符合 §六「高亮风险并给出整改建议」的输出要求。
 */
import { computed } from 'vue'
import type { RiskItem } from '@/api/types'
import { RISK_LABEL, RISK_TAG } from '@/utils/format'

const props = defineProps<{
  risks: RiskItem[]
  /** taskId → 任务名称 */
  taskNameOf: (taskId: string) => string
}>()

const emit = defineEmits<{ locate: [taskId: string] }>()

/** 按 HIGH → MEDIUM → LOW 排序展示 */
const ordered = computed(() => {
  const weight: Record<RiskItem['level'], number> = { HIGH: 0, MEDIUM: 1, LOW: 2 }
  return [...props.risks].sort((a, b) => weight[a.level] - weight[b.level])
})
</script>

<template>
  <div class="ip-risk-list">
    <el-empty v-if="ordered.length === 0" description="未检测到风险" :image-size="70" />
    <div
      v-for="risk in ordered"
      :key="risk.riskId"
      class="ip-risk"
      :class="`ip-risk--${risk.level.toLowerCase()}`"
    >
      <div class="ip-risk__head">
        <el-tag :type="RISK_TAG[risk.level]" size="small" :effect="risk.level === 'HIGH' ? 'dark' : 'plain'">
          {{ risk.level }} · {{ RISK_LABEL[risk.level] }}
        </el-tag>
        <span class="ip-risk__desc">{{ risk.description }}</span>
      </div>
      <div class="ip-risk__body">
        <span class="ip-muted">整改建议：</span>{{ risk.suggest }}
      </div>
      <div class="ip-risk__tasks">
        <span class="ip-muted">关联任务：</span>
        <template v-if="risk.taskIds.length === 0">
          <span class="ip-muted">—</span>
        </template>
        <el-tag
          v-for="taskId in risk.taskIds"
          :key="taskId"
          class="ip-risk__task"
          size="small"
          type="info"
          effect="plain"
          @click="emit('locate', taskId)"
        >
          {{ taskNameOf(taskId) }}
        </el-tag>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ip-risk-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-height: 420px;
  overflow: auto;
}

.ip-risk {
  background: #fafbfc;
  border: 1px solid var(--ip-border);
  border-radius: 8px;
  padding: 10px 12px;
}

.ip-risk__head {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}

.ip-risk__desc {
  font-size: 13px;
  font-weight: 600;
  line-height: 1.5;
}

.ip-risk__body {
  margin-top: 6px;
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--ip-text-secondary);
}

.ip-risk__tasks {
  margin-top: 8px;
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  font-size: 12px;
}

.ip-risk__task {
  cursor: pointer;
}
</style>
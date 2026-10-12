<script setup lang="ts">
/**
 * 风险缓解建议：聚合当前计划的风险清单，给出结构化行动项。
 *
 * 双模式（由后端 `/api/agent/advise` 决定并回传 mode）：
 *  - llm：已配置 LLM 网关时由模型基于「引擎风险结果 + 关键路径 + 指标」生成行动项；
 *  - rule：未配置 LLM 或模型调用异常时自动降级为规则聚合（引擎 suggest + 分级最佳实践）。
 *
 * 风险「识别」始终由引擎完成（`src/service/risk.ts` 硬编码规则 + 语义扫描），
 * Python 与前端只负责组织缓解建议，不参与风险判定。
 */
import { computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { usePlanStore } from '@/stores/plan'
import { RISK_LABEL, RISK_TAG } from '@/utils/format'

const store = usePlanStore()
const route = useRoute()
const router = useRouter()

const advice = computed(() => store.advice)

/** 跳转到任务管理页并定位目标任务 */
function locateTask(taskId: string): void {
  void router.push({ name: 'plan-tasks', params: { planId: route.params.planId as string }, query: { focus: taskId } })
}

function taskNameOf(taskId: string): string {
  return store.taskIndex.get(taskId)?.taskName ?? taskId.slice(0, 8)
}

onMounted(() => {
  void store.loadAgentHealth()
})
</script>

<template>
  <div class="ip-page">
    <el-empty v-if="!store.plan" description="请选择或新建一个迭代计划" />

    <template v-else>
      <div class="ip-panel">
        <div class="ip-panel__title">
          <span>风险缓解建议</span>
          <div class="ip-advisor-actions">
            <el-tag v-if="store.agentHealth" :type="store.agentHealth.enabled ? 'success' : 'info'" effect="plain" size="small">
              {{ store.agentHealth.enabled ? `LLM 已接入：${store.agentHealth.llm ?? '—'}` : '规则模式（未接入 LLM）' }}
            </el-tag>
            <el-button :icon="'Refresh'" :loading="store.adviceLoading" type="primary" @click="store.loadAdvice()">
              生成建议
            </el-button>
          </div>
        </div>
        <el-alert
          type="info"
          :closable="false"
          show-icon
          title="识别风险由算法引擎完成；本页基于引擎风险结果生成可执行的缓解建议。未配置 LLM 网关时自动降级为规则聚合，功能始终可用。"
        />
      </div>

      <el-empty
        v-if="!advice && !store.adviceLoading"
        description="点击「生成建议」按当前计划风险生成缓解方案"
      />

      <template v-if="advice">
        <div class="ip-panel">
          <div class="ip-panel__title">
            <span>总体建议</span>
            <el-tag size="small" :type="advice.mode === 'llm' ? 'success' : 'info'" effect="plain">
              {{ advice.mode === 'llm' ? 'LLM 生成' : '规则聚合' }}
            </el-tag>
          </div>
          <p class="ip-advisor-summary">{{ advice.summary }}</p>
          <el-alert
            v-if="advice.degradedReason"
            type="warning"
            :closable="false"
            show-icon
            :title="`已降级为规则建议：${advice.degradedReason}`"
          />
        </div>

        <el-empty v-if="advice.items.length === 0" description="当前计划未检测到风险，无需额外缓解措施" />

        <div v-else class="ip-advisor-list">
          <div
            v-for="item in advice.items"
            :key="item.riskId"
            class="ip-panel ip-advisor-item"
            :class="`ip-risk--${item.level.toLowerCase()}`"
          >
            <div class="ip-panel__title">
              <span class="ip-advisor-item__title">风险项</span>
              <el-tag size="small" :type="RISK_TAG[item.level]" effect="dark">{{ RISK_LABEL[item.level] }}</el-tag>
            </div>

            <div class="ip-advisor-item__desc">{{ item.description }}</div>

            <div v-if="item.taskIds.length > 0" class="ip-advisor-item__tasks">
              <span class="ip-muted">关联任务：</span>
              <el-tag
                v-for="taskId in item.taskIds"
                :key="taskId"
                size="small"
                effect="plain"
                type="primary"
                class="ip-advisor-item__task"
                @click="locateTask(taskId)"
              >
                {{ taskNameOf(taskId) }}
              </el-tag>
            </div>

            <div class="ip-advisor-item__block">
              <div class="ip-advisor-item__block-title">引擎建议</div>
              <div class="ip-advisor-item__suggest">{{ item.suggestion }}</div>
            </div>

            <div v-if="item.actions.length > 0" class="ip-advisor-item__block">
              <div class="ip-advisor-item__block-title">行动项</div>
              <ol class="ip-advisor-item__actions">
                <li v-for="(action, index) in item.actions" :key="index">{{ action }}</li>
              </ol>
            </div>
          </div>
        </div>
      </template>
    </template>
  </div>
</template>

<style scoped>
.ip-advisor-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.ip-advisor-summary {
  margin: 0;
  line-height: 1.8;
  font-size: 13.5px;
  white-space: pre-wrap;
  word-break: break-word;
}

.ip-advisor-list {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.ip-advisor-item {
  border-left-width: 4px;
}

.ip-advisor-item__title {
  font-size: 14px;
}

.ip-advisor-item__desc {
  font-size: 13.5px;
  line-height: 1.7;
  margin-bottom: 10px;
}

.ip-advisor-item__tasks {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 12px;
  font-size: 12.5px;
}

.ip-advisor-item__task {
  cursor: pointer;
}

.ip-advisor-item__block {
  margin-top: 10px;
}

.ip-advisor-item__block-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--ip-text-secondary);
  margin-bottom: 4px;
}

.ip-advisor-item__suggest {
  font-size: 13px;
  line-height: 1.7;
  background: #f7f9fc;
  border-radius: 6px;
  padding: 8px 12px;
}

.ip-advisor-item__actions {
  margin: 0;
  padding-left: 20px;
  font-size: 13px;
  line-height: 1.9;
}
</style>
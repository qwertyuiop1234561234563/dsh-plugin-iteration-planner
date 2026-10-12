<script setup lang="ts">
/**
 * 计划 KPI 卡片组：任务规模、工时、关键路径工期、风险、迭代窗口。
 */
import { computed } from 'vue'
import type { ProjectPlan } from '@/api/types'
import { hoursToWorkDays } from '@/utils/format'

const props = defineProps<{ plan: ProjectPlan }>()

const stats = computed(() => {
  const risks = props.plan.riskList
  return {
    taskCount: props.plan.taskList.length,
    totalHours: props.plan.totalEstimatedHours,
    duration: props.plan.totalDurationHours,
    workDays: hoursToWorkDays(props.plan.totalDurationHours),
    criticalCount: props.plan.criticalPath.length,
    high: risks.filter((r) => r.level === 'HIGH').length,
    medium: risks.filter((r) => r.level === 'MEDIUM').length,
    low: risks.filter((r) => r.level === 'LOW').length,
    riskTotal: risks.length,
    devCount: props.plan.devMemberCount,
  }
})
</script>

<template>
  <div class="ip-kpi">
    <div class="ip-kpi__card">
      <div class="ip-kpi__label">任务总数</div>
      <div class="ip-kpi__value">{{ stats.taskCount }}</div>
      <div class="ip-kpi__hint">关键路径 {{ stats.criticalCount }} 项</div>
    </div>
    <div class="ip-kpi__card">
      <div class="ip-kpi__label">总预估工时</div>
      <div class="ip-kpi__value">{{ stats.totalHours }}<span class="ip-kpi__unit">h</span></div>
      <div class="ip-kpi__hint">含缓冲修正后口径</div>
    </div>
    <div class="ip-kpi__card">
      <div class="ip-kpi__label">关键路径工期</div>
      <div class="ip-kpi__value">{{ stats.duration }}<span class="ip-kpi__unit">h</span></div>
      <div class="ip-kpi__hint">约 {{ stats.workDays }}</div>
    </div>
    <div class="ip-kpi__card" :class="{ 'ip-kpi__card--alert': stats.high > 0 }">
      <div class="ip-kpi__label">风险条目</div>
      <div class="ip-kpi__value">{{ stats.riskTotal }}</div>
      <div class="ip-kpi__hint">
        <el-tag v-if="stats.high" type="danger" size="small" effect="dark">HIGH {{ stats.high }}</el-tag>
        <el-tag v-if="stats.medium" type="warning" size="small" effect="plain" style="margin-left: 4px">
          MEDIUM {{ stats.medium }}
        </el-tag>
        <el-tag v-if="stats.low" type="info" size="small" effect="plain" style="margin-left: 4px">LOW {{ stats.low }}</el-tag>
        <span v-if="stats.riskTotal === 0" class="ip-muted">未检出风险</span>
      </div>
    </div>
    <div class="ip-kpi__card">
      <div class="ip-kpi__label">迭代窗口</div>
      <div class="ip-kpi__value ip-kpi__value--small">
        {{ plan.iterationStartDate }} → {{ plan.iterationDeadline }}
      </div>
      <div class="ip-kpi__hint">可用开发 {{ stats.devCount }} 人</div>
    </div>
  </div>
</template>

<style scoped>
.ip-kpi {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 14px;
}

.ip-kpi__card {
  position: relative;
  background: var(--ip-surface);
  border: 1px solid var(--ip-border);
  border-radius: var(--ip-radius);
  box-shadow: var(--ip-shadow-sm);
  padding: 16px 18px;
  overflow: hidden;
}

/* 卡片左上角的制图刻痕 */
.ip-kpi__card::after {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  width: 34px;
  height: 2px;
  background: var(--ip-brand);
}

.ip-kpi__card--alert {
  border-color: rgba(214, 69, 69, 0.45);
  background: linear-gradient(180deg, #fbecec 0%, #ffffff 62%);
}

.ip-kpi__card--alert::after {
  background: var(--ip-danger);
}

.ip-kpi__label {
  font-family: var(--ip-font-mono);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--ip-text-muted);
}

.ip-kpi__value {
  margin-top: 8px;
  font-family: var(--ip-font-display);
  font-size: 28px;
  font-weight: 700;
  line-height: 1.15;
  letter-spacing: -0.01em;
  font-variant-numeric: tabular-nums;
  color: var(--ip-ink-900);
}

.ip-kpi__value--small {
  font-size: 15px;
  font-weight: 600;
}

.ip-kpi__unit {
  font-size: 14px;
  font-weight: 500;
  margin-left: 2px;
  color: var(--ip-text-secondary);
}

.ip-kpi__hint {
  margin-top: 8px;
  font-size: 12px;
  color: var(--ip-text-secondary);
  min-height: 20px;
  display: flex;
  align-items: center;
  gap: 4px;
  flex-wrap: wrap;
}
</style>
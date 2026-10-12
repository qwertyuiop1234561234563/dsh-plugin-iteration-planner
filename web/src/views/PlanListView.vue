<script setup lang="ts">
/**
 * 计划列表页：展示全部迭代计划摘要，提供查看 / 删除入口。
 */
import { onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { usePlanStore } from '@/stores/plan'
import { formatTime, hoursToWorkDays, truncate } from '@/utils/format'

const store = usePlanStore()
const router = useRouter()

onMounted(() => {
  void store.loadPlans()
})

function open(planId: string, target: 'plan-overview' | 'plan-tasks' | 'plan-dag'): void {
  void router.push({ name: target, params: { planId } })
}

async function confirmRemove(planId: string, requirement: string): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确认删除该迭代计划？「${truncate(requirement, 32)}」删除后不可恢复。`,
      '删除确认',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return // 用户取消
  }
  await store.removePlan(planId)
}
</script>

<template>
  <div class="ip-page">
    <div class="ip-page__actions">
      <el-button type="primary" :icon="'MagicStick'" @click="router.push({ name: 'plan-create' })">
        新建迭代计划
      </el-button>
      <el-button :icon="'Refresh'" @click="store.loadPlans()">刷新</el-button>
      <span class="ip-muted">共 {{ store.summaries.length }} 个计划</span>
    </div>

    <div class="ip-panel">
      <el-table
        v-loading="store.listLoading"
        :data="store.summaries"
        stripe
        empty-text="暂无计划，请先新建一个迭代计划"
      >
        <el-table-column type="index" label="#" width="52" />
        <el-table-column label="原始需求" min-width="260">
          <template #default="{ row }">
            <el-link type="primary" underline="never" @click="open(row.planId, 'plan-overview')">
              {{ truncate(row.rawRequirement, 56) }}
            </el-link>
          </template>
        </el-table-column>
        <el-table-column label="迭代窗口" width="210">
          <template #default="{ row }">{{ row.iterationStartDate }} ~ {{ row.iterationDeadline }}</template>
        </el-table-column>
        <el-table-column label="开发人数" prop="devMemberCount" width="90" align="center" />
        <el-table-column label="任务数" prop="taskCount" width="80" align="center" />
        <el-table-column label="总工时" width="96" align="center">
          <template #default="{ row }">{{ row.totalEstimatedHours }}h</template>
        </el-table-column>
        <el-table-column label="关键路径工期" width="150" align="center">
          <template #default="{ row }">{{ row.totalDurationHours }}h（{{ hoursToWorkDays(row.totalDurationHours) }}）</template>
        </el-table-column>
        <el-table-column label="风险" width="130" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.riskCount === 0" type="success" size="small" effect="plain">无</el-tag>
            <span v-else>
              <el-tag v-if="row.highRiskCount > 0" type="danger" size="small" effect="dark">
                HIGH {{ row.highRiskCount }}
              </el-tag>
              <el-tag type="info" size="small" effect="plain" style="margin-left: 4px">{{ row.riskCount }}</el-tag>
            </span>
          </template>
        </el-table-column>
        <el-table-column label="更新时间" width="170">
          <template #default="{ row }">{{ formatTime(row.updatedAt) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="230" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="open(row.planId, 'plan-overview')">总览</el-button>
            <el-button link type="primary" size="small" @click="open(row.planId, 'plan-tasks')">任务</el-button>
            <el-button link type="primary" size="small" @click="open(row.planId, 'plan-dag')">DAG</el-button>
            <el-button link type="danger" size="small" @click="confirmRemove(row.planId, row.rawRequirement)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<style scoped>
.ip-page__actions {
  display: flex;
  align-items: center;
  gap: 12px;
}
</style>
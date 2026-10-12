<script setup lang="ts">
/**
 * 审计日志页：展示全链路工具调用与重规划事件的落库记录。
 *
 * 每条记录包含动作名、成功标记、耗时、入参与返回值；失败记录额外展示错误信息。
 * 支持按计划过滤、变更条数上限与手动刷新，用于问题追溯与合规审计。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { usePlanStore } from '@/stores/plan'
import { formatIso } from '@/utils/format'

const store = usePlanStore()

const planFilter = ref<string>('')
const limit = ref(300)
const loading = ref(false)

/** 审计记录按时间倒序（后端返回近实时顺序，这里再做一次稳定排序） */
const rows = computed(() =>
  [...store.audit].sort((a, b) => new Date(b.ts).getTime() - new Date(a.ts).getTime()),
)

const stats = computed(() => {
  const list = rows.value
  const failed = list.filter((item) => !item.ok).length
  const totalMs = list.reduce((sum, item) => sum + (item.durationMs || 0), 0)
  return {
    total: list.length,
    failed,
    avgMs: list.length > 0 ? Math.round(totalMs / list.length) : 0,
  }
})

const planOptions = computed(() =>
  store.summaries.map((item) => ({ value: item.planId, label: `${item.rawRequirement.slice(0, 30)}` })),
)

async function reload(): Promise<void> {
  loading.value = true
  try {
    await store.loadAudit(planFilter.value || undefined)
  } finally {
    loading.value = false
  }
}

/** 格式化 JSON 片段（截断超长内容，避免单条记录撑爆页面） */
function pretty(value: unknown, max = 4000): string {
  if (value === undefined || value === null) return '—'
  let text: string
  try {
    text = typeof value === 'string' ? value : JSON.stringify(value, null, 2)
  } catch {
    text = String(value)
  }
  return text.length > max ? `${text.slice(0, max)}\n…（已截断）` : text
}

watch([planFilter, limit], reload)
onMounted(reload)
</script>

<template>
  <div class="ip-page">
    <div class="ip-panel">
      <div class="ip-panel__title">
        <span>审计日志</span>
        <div class="ip-actions">
          <el-select v-model="planFilter" placeholder="全部计划" clearable filterable style="width: 260px">
            <el-option v-for="item in planOptions" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
          <el-select v-model="limit" style="width: 120px">
            <el-option :value="100" label="最近 100" />
            <el-option :value="300" label="最近 300" />
            <el-option :value="1000" label="最近 1000" />
          </el-select>
          <el-button :icon="'Refresh'" :loading="loading" @click="reload">刷新</el-button>
        </div>
      </div>

      <div class="ip-stats">
        <el-tag type="info" effect="plain" size="large">共 {{ stats.total }} 条</el-tag>
        <el-tag :type="stats.failed > 0 ? 'danger' : 'success'" effect="plain" size="large">
          失败 {{ stats.failed }} 条
        </el-tag>
        <el-tag type="info" effect="plain" size="large">平均耗时 {{ stats.avgMs }} ms</el-tag>
      </div>

      <el-table
        v-loading="loading"
        :data="rows"
        stripe
        height="640"
        empty-text="暂无审计记录（执行任意计划操作后会在此留痕）"
      >
        <el-table-column type="expand">
          <template #default="{ row }">
            <div class="ip-audit-detail">
              <div class="ip-audit-detail__col">
                <div class="ip-audit-detail__label">入参（input）</div>
                <pre class="ip-mono ip-audit-detail__code">{{ pretty(row.input) }}</pre>
              </div>
              <div class="ip-audit-detail__col">
                <div class="ip-audit-detail__label">返回（output）</div>
                <pre class="ip-mono ip-audit-detail__code">{{ pretty(row.output) }}</pre>
              </div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="时间" width="180">
          <template #default="{ row }">{{ formatIso(row.ts) }}</template>
        </el-table-column>
        <el-table-column label="动作" min-width="180">
          <template #default="{ row }"><span class="ip-mono">{{ row.action }}</span></template>
        </el-table-column>
        <el-table-column label="结果" width="88" align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="row.ok ? 'success' : 'danger'" effect="plain">
              {{ row.ok ? '成功' : '失败' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="耗时" width="100" align="right">
          <template #default="{ row }">
            <span :class="{ 'ip-slow': row.durationMs > 3000 }">{{ row.durationMs }} ms</span>
          </template>
        </el-table-column>
        <el-table-column label="计划 ID" width="200">
          <template #default="{ row }"><span class="ip-mono ip-muted">{{ row.planId ?? '—' }}</span></template>
        </el-table-column>
        <el-table-column label="错误信息" min-width="220">
          <template #default="{ row }">
            <span v-if="row.error" class="ip-error">{{ row.error }}</span>
            <span v-else class="ip-muted">—</span>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<style scoped>
.ip-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.ip-stats {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 14px;
}

.ip-audit-detail {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
  gap: 14px;
  padding: 6px 4px;
}

.ip-audit-detail__label {
  font-size: 12px;
  font-weight: 600;
  color: var(--ip-text-secondary);
  margin-bottom: 6px;
}

.ip-audit-detail__code {
  margin: 0;
  max-height: 260px;
  overflow: auto;
  background: #fafbfc;
  border: 1px solid var(--ip-border);
  border-radius: 8px;
  padding: 10px 12px;
  white-space: pre-wrap;
  word-break: break-word;
}

.ip-slow {
  color: var(--ip-warning);
  font-weight: 600;
}

.ip-error {
  color: var(--ip-danger);
}
</style>
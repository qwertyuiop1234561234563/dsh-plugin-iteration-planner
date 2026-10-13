<script setup lang="ts">
/**
 * 活动时间线：把当前计划的全链路审计记录（引擎 `GET /plans/:planId/audit`）按时间倒序呈现。
 *
 * 审计记录由引擎在每个工具调用 / DAG 变更 / 重规划动作时落库（含入参、返回与耗时），
 * 本页只做展示与筛选，不产生任何业务写入。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import type { AuditRecord } from '@/api/types'
import { usePlanStore } from '@/stores/plan'
import { formatIso } from '@/utils/format'

const store = usePlanStore()
const route = useRoute()

const planId = computed(() => (route.params.planId as string | undefined) ?? '')

/** 审计动作 → 可读文案（未知动作回退为原始 action） */
const ACTION_LABEL: Record<string, string> = {
  plan_iteration: '完整规划流水线',
  task_decompose: '需求拆解',
  'task_decompose.degrade': '需求拆解降级（规则兜底）',
  replan_all: '全量重规划',
  task_add: '新增任务',
  task_update: '修改任务',
  task_delete: '删除任务',
  risk_scan: '风险扫描',
  'risk_scan.degrade': '风险扫描降级（规则兜底）',
  'dag_check.dangling': '悬空依赖提示',
  report_generate: '生成报表',
  'api.startup': '服务启动',
}

function actionLabel(action: string): string {
  return ACTION_LABEL[action] ?? action
}

const failedOnly = ref(false)
const keyword = ref('')

/** 过滤 + 倒序（ts 为 ISO 字符串，可直接字典序排序） */
const records = computed(() => {
  const lowered = keyword.value.trim().toLowerCase()
  return [...store.audit]
    .filter((record) => (failedOnly.value ? !record.ok : true))
    .filter((record) =>
      !lowered ? true : record.action.toLowerCase().includes(lowered) || actionLabel(record.action).includes(lowered),
    )
    .sort((a, b) => (a.ts < b.ts ? 1 : -1))
})

const failureCount = computed(() => store.audit.filter((record) => !record.ok).length)

/* ------------------------------- 详情弹窗 ------------------------------- */

const detailVisible = ref(false)
const detailRecord = ref<AuditRecord | null>(null)

function openDetail(record: AuditRecord): void {
  detailRecord.value = record
  detailVisible.value = true
}

function pretty(value: unknown): string {
  if (value === undefined || value === null) return '—'
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

/* ------------------------------- 数据加载 ------------------------------- */

async function load(): Promise<void> {
  if (!planId.value) return
  await store.loadAudit(planId.value)
}

onMounted(load)
watch(planId, load)
</script>

<template>
  <div class="ip-page">
    <div class="ip-panel">
      <div class="ip-panel__title">
        <span>活动时间线</span>
        <div class="ip-timeline-actions">
          <el-input
            v-model="keyword"
            :prefix-icon="'Search'"
            placeholder="按动作筛选，如 replan / risk"
            clearable
            style="width: 240px"
          />
          <el-checkbox v-model="failedOnly">仅失败</el-checkbox>
          <el-tag v-if="failureCount > 0" type="danger" effect="plain" size="small">失败 {{ failureCount }}</el-tag>
          <el-button :icon="'Refresh'" @click="load">刷新</el-button>
        </div>
      </div>

      <el-empty
        v-if="records.length === 0"
        :description="store.audit.length === 0 ? '暂无审计记录（执行规划或重规划后生成）' : '无匹配记录'"
      />

      <el-timeline v-else style="padding-left: 4px">
        <el-timeline-item
          v-for="record in records"
          :key="record.recordId"
          :timestamp="formatIso(record.ts)"
          placement="top"
          :color="record.ok ? 'var(--ip-success)' : 'var(--ip-danger)'"
          :hollow="false"
        >
          <div class="ip-timeline-item" :class="{ 'is-failed': !record.ok }">
            <div class="ip-timeline-item__head">
              <span class="ip-timeline-item__action">{{ actionLabel(record.action) }}</span>
              <el-tag size="small" effect="plain" :type="record.ok ? 'success' : 'danger'">
                {{ record.ok ? '成功' : '失败' }}
              </el-tag>
              <el-tag size="small" effect="plain" type="info">{{ record.durationMs }} ms</el-tag>
              <span class="ip-mono ip-muted">{{ record.action }}</span>
              <el-button link type="primary" size="small" @click="openDetail(record)">详情</el-button>
            </div>
            <div v-if="record.error" class="ip-timeline-item__error">{{ record.error }}</div>
          </div>
        </el-timeline-item>
      </el-timeline>
    </div>

    <el-dialog v-model="detailVisible" title="审计明细" width="680px">
      <template v-if="detailRecord">
        <div class="ip-detail-row"><span class="ip-muted">动作</span>{{ actionLabel(detailRecord.action) }}</div>
        <div class="ip-detail-row"><span class="ip-muted">时间</span>{{ formatIso(detailRecord.ts) }}</div>
        <div class="ip-detail-row"><span class="ip-muted">耗时</span>{{ detailRecord.durationMs }} ms</div>
        <el-divider content-position="left">入参</el-divider>
        <pre class="ip-markdown">{{ pretty(detailRecord.input) }}</pre>
        <el-divider content-position="left">返回</el-divider>
        <pre class="ip-markdown">{{ pretty(detailRecord.output) }}</pre>
        <template v-if="detailRecord.error">
          <el-divider content-position="left">错误</el-divider>
          <pre class="ip-markdown">{{ detailRecord.error }}</pre>
        </template>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.ip-timeline-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.ip-timeline-item {
  padding-bottom: 4px;
}

.ip-timeline-item__head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.ip-timeline-item__action {
  font-size: 13.5px;
  font-weight: 600;
}

.ip-timeline-item__error {
  margin-top: 6px;
  font-size: 12px;
  color: var(--ip-danger);
  background: color-mix(in srgb, var(--ip-danger) 10%, transparent);
  border-radius: 6px;
  padding: 6px 10px;
}

.ip-detail-row {
  display: flex;
  gap: 12px;
  font-size: 13px;
  margin-bottom: 6px;
}

.ip-detail-row .ip-muted {
  flex: 0 0 48px;
}
</style>
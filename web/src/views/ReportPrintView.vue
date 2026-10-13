<script setup lang="ts">
/**
 * 打印友好报告页：把计划、关键路径、风险、里程碑与迭代报表汇总为一份可交付文档。
 *
 * 说明：
 *  - 版式由 `styles/main.css` 的 `@media print` 统一约束（打印时自动隐藏侧栏 / 抬头 /
 *    概况带与工具条），因此本页无需额外维护一套打印样式；
 *  - 所有数值均取自引擎返回值与既有报表，前端只做排版组织。
 */
import { computed, nextTick, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import type { Task } from '@/api/types'
import KpiCards from '@/components/KpiCards.vue'
import { usePlanStore } from '@/stores/plan'
import { detectMilestones, taskDateRange } from '@/utils/analytics'
import { RISK_LABEL, STATUS_LABEL, TASK_TYPE_LABEL, formatTime, truncate } from '@/utils/format'

const store = usePlanStore()
const route = useRoute()

/** 生成时间（报告页脚） */
const generatedAt = ref(formatTime(Date.now()))

const plan = computed(() => store.plan)

/** 关键路径任务（按引擎给出的顺序） */
const criticalTasks = computed<Task[]>(() => {
  const current = plan.value
  if (!current) return []
  return current.criticalPath
    .map((taskId) => store.taskIndex.get(taskId))
    .filter((task): task is Task => task !== undefined)
})

/** 里程碑清单（展示性推断，见 utils/analytics） */
const milestones = computed(() => detectMilestones(plan.value))

/** 本计划的审计摘要（最近 12 条） */
const auditDigest = computed(() => store.audit.filter((record) => record.planId === plan.value?.planId).slice(0, 12))

/** 任务在日历上的排期（展示换算） */
function scheduleRange(taskId: string): string {
  const current = plan.value
  const task = store.taskIndex.get(taskId)
  if (!current || !task) return '—'
  const range = taskDateRange(current, task)
  return `${range.start} → ${range.end}`
}

/** 打印：等待渲染完成后唤起系统打印 */
function printNow(): void {
  void nextTick(() => window.print())
}

/** 刷新计划详情与审计摘要（打印前校对最新数据） */
async function refresh(): Promise<void> {
  await store.refreshCurrent()
  await store.loadAudit(route.params.planId as string | undefined)
  generatedAt.value = formatTime(Date.now())
}

onMounted(async () => {
  const planId = route.params.planId as string | undefined
  if (planId && store.currentPlanId !== planId) await store.loadPlan(planId)
  await store.loadAudit(planId)
  generatedAt.value = formatTime(Date.now())
  // 由快捷键（Ctrl/⌘ + Shift + H）进入时自动唤起打印
  if (route.query.autoprint === '1') {
    await nextTick()
    window.setTimeout(printNow, 500)
  }
})
</script>

<template>
  <div class="ip-page ip-print">
    <div class="ip-page__actions ip-no-print">
      <el-button type="primary" :icon="'Printer'" @click="printNow">打印 / 导出 PDF</el-button>
      <el-button :icon="'Refresh'" @click="refresh">刷新数据</el-button>
      <span class="ip-muted">打印时会自动隐藏控制台外框，仅保留报告内容</span>
    </div>

    <template v-if="plan">
      <!-- 报告抬头 -->
      <div class="ip-panel ip-print__cover">
        <div class="ip-print__cover-head">
          <div>
            <div class="ip-print__cover-eyebrow">迭代规划报告 · Iteration Planning Report</div>
            <h2 class="ip-print__cover-title">{{ truncate(plan.rawRequirement, 120) }}</h2>
          </div>
          <div class="ip-print__cover-meta">
            <div><span>计划编号</span>{{ plan.planId }}</div>
            <div><span>迭代窗口</span>{{ plan.iterationStartDate }} ~ {{ plan.iterationDeadline }}</div>
            <div><span>可用开发</span>{{ plan.devMemberCount }} 人</div>
            <div><span>生成时间</span>{{ generatedAt }}</div>
          </div>
        </div>
      </div>

      <KpiCards :plan="plan" />

      <!-- 关键路径 -->
      <div class="ip-panel">
        <div class="ip-panel__title">关键路径任务（AOE 计算结果）</div>
        <el-table :data="criticalTasks" size="small" stripe>
          <el-table-column type="index" label="#" width="48" />
          <el-table-column label="任务名称" prop="taskName" min-width="200" />
          <el-table-column label="类型" width="100">
            <template #default="{ row }">{{ TASK_TYPE_LABEL[row.taskType as keyof typeof TASK_TYPE_LABEL] }}</template>
          </el-table-column>
          <el-table-column label="状态" width="90">
            <template #default="{ row }">{{ STATUS_LABEL[row.status as keyof typeof STATUS_LABEL] }}</template>
          </el-table-column>
          <el-table-column label="责任人" width="96">
            <template #default="{ row }">{{ row.assignee ?? '待分配' }}</template>
          </el-table-column>
          <el-table-column label="工时" prop="estimatedHours" width="80" align="right" />
          <el-table-column label="排期（工作日）" width="200">
            <template #default="{ row }">{{ scheduleRange(row.taskId) }}</template>
          </el-table-column>
        </el-table>
        <p class="ip-muted" style="margin: 8px 0 0">
          共 {{ criticalTasks.length }} 项关键任务，关键工期 {{ plan.totalDurationHours }}h；松弛时间为 0 的任务即关键任务。
        </p>
      </div>

      <!-- 里程碑 -->
      <div class="ip-panel">
        <div class="ip-panel__title">里程碑节点（自动标记）</div>
        <el-table v-if="milestones.length > 0" :data="milestones" size="small" stripe>
          <el-table-column label="类型" width="110">
            <template #default="{ row }">
              <el-tag :type="row.kind === 'RELEASE' ? 'danger' : 'primary'" size="small" effect="plain">
                {{ row.kind === 'RELEASE' ? '发布里程碑' : '交付里程碑' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="任务" prop="taskName" min-width="220" />
          <el-table-column label="计划日期" prop="date" width="130" />
        </el-table>
        <el-empty v-else description="当前计划未识别出里程碑任务" :image-size="60" />
      </div>

      <!-- 风险清单 -->
      <div class="ip-panel">
        <div class="ip-panel__title">风险清单（引擎风险扫描结果）</div>
        <el-table v-if="plan.riskList.length > 0" :data="plan.riskList" size="small" stripe>
          <el-table-column label="等级" width="90">
            <template #default="{ row }">
              <el-tag :type="row.level === 'HIGH' ? 'danger' : row.level === 'MEDIUM' ? 'warning' : 'info'" size="small">
                {{ RISK_LABEL[row.level as keyof typeof RISK_LABEL] }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="风险描述" prop="description" min-width="280" />
          <el-table-column label="缓解建议" prop="suggest" min-width="280" />
        </el-table>
        <el-empty v-else description="未检出风险" :image-size="60" />
      </div>

      <!-- 全量任务 -->
      <div class="ip-panel">
        <div class="ip-panel__title">任务清单（全量 {{ plan.taskList.length }} 项）</div>
        <el-table :data="plan.taskList" size="small" stripe>
          <el-table-column type="index" label="#" width="48" />
          <el-table-column label="任务名称" prop="taskName" min-width="200" />
          <el-table-column label="类型" width="100">
            <template #default="{ row }">{{ TASK_TYPE_LABEL[row.taskType as keyof typeof TASK_TYPE_LABEL] }}</template>
          </el-table-column>
          <el-table-column label="状态" width="90">
            <template #default="{ row }">{{ STATUS_LABEL[row.status as keyof typeof STATUS_LABEL] }}</template>
          </el-table-column>
          <el-table-column label="优先级" prop="priority" width="80" align="center" />
          <el-table-column label="责任人" width="96">
            <template #default="{ row }">{{ row.assignee ?? '待分配' }}</template>
          </el-table-column>
          <el-table-column label="工时" prop="estimatedHours" width="80" align="right" />
          <el-table-column label="松弛" prop="slack" width="80" align="right" />
        </el-table>
      </div>

      <!-- 迭代报表原文 -->
      <div class="ip-panel">
        <div class="ip-panel__title">迭代报表（引擎输出）</div>
        <pre class="ip-markdown">{{ store.report?.content ?? '报表暂不可用' }}</pre>
      </div>

      <!-- 审计摘要 -->
      <div class="ip-panel">
        <div class="ip-panel__title">变更与审计摘要（最近 {{ auditDigest.length }} 条）</div>
        <el-table v-if="auditDigest.length > 0" :data="auditDigest" size="small" stripe>
          <el-table-column label="时间" width="180">
            <template #default="{ row }">{{ formatTime(Date.parse(row.ts)) }}</template>
          </el-table-column>
          <el-table-column label="动作" prop="action" width="150" />
          <el-table-column label="结果" width="80">
            <template #default="{ row }">
              <el-tag :type="row.ok ? 'success' : 'danger'" size="small" effect="plain">{{ row.ok ? '成功' : '失败' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="耗时" width="90">
            <template #default="{ row }">{{ row.durationMs }}ms</template>
          </el-table-column>
        </el-table>
        <el-empty v-else description="暂无审计记录" :image-size="60" />
      </div>

      <div class="ip-print__footer">
        本报告由迭代规划控制台自动生成 · 计划编号 {{ plan.planId }} · 生成时间 {{ generatedAt }}
      </div>
    </template>

    <el-empty v-else description="尚未选择计划，请先在顶部选择或新建一个迭代计划" />
  </div>
</template>

<style scoped>
.ip-print {
  max-width: 1180px;
  margin: 0 auto;
}

.ip-print__cover-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 24px;
  flex-wrap: wrap;
}

.ip-print__cover-eyebrow {
  font-family: var(--ip-font-mono);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--ip-text-muted);
}

.ip-print__cover-title {
  margin: 6px 0 0;
  font-family: var(--ip-font-display);
  font-size: 22px;
  font-weight: 700;
  line-height: 1.35;
  color: var(--ip-ink-900);
}

.ip-print__cover-meta {
  display: grid;
  grid-template-columns: repeat(2, minmax(160px, auto));
  gap: 6px 22px;
  font-family: var(--ip-font-mono);
  font-size: 12px;
  color: var(--ip-text);
}

.ip-print__cover-meta span {
  display: inline-block;
  min-width: 62px;
  margin-right: 8px;
  color: var(--ip-text-muted);
}

.ip-print__footer {
  padding: 10px 4px 0;
  border-top: 1px solid var(--ip-border);
  font-family: var(--ip-font-mono);
  font-size: 11px;
  color: var(--ip-text-muted);
  text-align: right;
}

/* 深色模式下同样使用语义文本色（--ip-ink-900 在夜间不可读） */
:global(html[data-theme='dark']) .ip-print__cover-title {
  color: var(--ip-text);
}
</style>

<script setup lang="ts">
/**
 * 计划总览页：KPI 概览 + 关键路径时间线 + 风险面板 + 燃尽图 + 迭代报表 + 导出 / 重规划。
 *
 * 数据来源：store.plan（详情）、store.graph（DAG 视图）、store.report（Markdown 报表）。
 * 重规划会完整重跑后端流水线（环检测 → AOE 排期 → 风险扫描），成功后自动刷新全部视图。
 */
import { computed, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import type { ProjectPlan } from '@/api/types'
import BurndownChart from '@/components/BurndownChart.vue'
import KpiCards from '@/components/KpiCards.vue'
import RiskPanel from '@/components/RiskPanel.vue'
import { usePlanStore } from '@/stores/plan'
import { detectMilestones } from '@/utils/analytics'
import { TASK_TYPE_COLOR, TASK_TYPE_LABEL, formatTime, hoursToWorkDays } from '@/utils/format'

const store = usePlanStore()
const route = useRoute()
const router = useRouter()

/** 当前计划（为空时展示空态） */
const plan = computed<ProjectPlan | null>(() => store.plan)

/** 关键路径任务：按最早开始时间升序，形成可读的执行时间线 */
const criticalTasks = computed(() => {
  const current = plan.value
  if (!current) return []
  const set = new Set(current.criticalPath)
  return current.taskList.filter((task) => set.has(task.taskId)).sort((a, b) => a.es - b.es)
})

/** 时间线最大 es，用于进度条比例 */
const maxEs = computed(() => criticalTasks.value.reduce((max, task) => Math.max(max, task.es + task.estimatedHours), 1))

/** 计划最后更新时间（取列表摘要，缺失时回退占位符） */
const lastUpdated = computed(() => {
  const current = plan.value
  if (!current) return '—'
  const summary = store.summaries.find((item) => item.planId === current.planId)
  return summary ? formatTime(summary.updatedAt) : '—'
})

/** 风险条目按 ID 查任务名称 */
function taskNameOf(taskId: string): string {
  return store.taskIndex.get(taskId)?.taskName ?? taskId
}

/** 里程碑节点（只读推断：发布任务 / 末端交付任务，不参与排期判定） */
const milestones = computed(() => detectMilestones(plan.value))

/** 定位风险关联任务：跳转任务表并携带聚焦参数 */
function locateTask(taskId: string): void {
  if (!plan.value) return
  void router.push({ name: 'plan-tasks', params: { planId: plan.value.planId }, query: { focus: taskId } })
}

/* ------------------------------- 重规划 ------------------------------- */

const replanVisible = ref(false)
const replanForm = reactive({ iterationStartDate: '', iterationDeadline: '', devMemberCount: 1 })

/** 打开重规划弹窗时以当前计划参数预填 */
function openReplan(): void {
  const current = plan.value
  if (!current) return
  replanForm.iterationStartDate = current.iterationStartDate
  replanForm.iterationDeadline = current.iterationDeadline
  replanForm.devMemberCount = current.devMemberCount
  replanVisible.value = true
}

async function submitReplan(): Promise<void> {
  if (replanForm.iterationDeadline < replanForm.iterationStartDate) return
  // 先关弹窗：重规划是异步作业，可能跑几分钟，把用户晾在模态框里等是不合理的
  replanVisible.value = false
  await store.replan({
    iterationStartDate: replanForm.iterationStartDate,
    iterationDeadline: replanForm.iterationDeadline,
    devMemberCount: replanForm.devMemberCount,
  })
}

/* --------------------------- 重规划作业进度（展示态） --------------------------- */

/** 阶段 → 百分比（与 PlanCreateView 同口径：阶段离散，进度条只表达「走到哪一段」） */
const PHASE_PERCENT: Record<string, number> = { queued: 10, engine: 60, settled: 90 }

const jobStatusText = computed(() => (store.writeJob?.status === 'running' ? '处理中' : '排队中'))

const jobProgressStatus = computed(() =>
  store.writeJob?.status === 'failed' ? ('exception' as const) : undefined,
)

/** 进度百分比：阶段离散，进度条只表达「走到哪一段」 */
const jobPercent = computed(() => {
  const job = store.writeJob
  if (!job) return 0
  if (job.status === 'succeeded') return 100
  const last = job.progress[job.progress.length - 1]
  if (!last) return job.status === 'pending' ? 2 : 5
  return (PHASE_PERCENT as Record<string, number>)[last.phase] ?? 60
})

const cancellable = computed(() => {
  const status = store.writeJob?.status
  return status === 'pending' || status === 'running'
})

/** 切换计划时关闭遗留弹窗 */
watch(
  () => route.params.planId,
  () => {
    replanVisible.value = false
  },
)
</script>

<template>
  <div class="ip-page">
    <el-empty v-if="!plan" description="请选择或新建一个迭代计划" />

    <template v-else>
      <!-- 顶部：需求描述 + 全局操作 -->
      <div class="ip-panel">
        <div class="ip-panel__title">
          <span>迭代需求</span>
          <div class="ip-actions">
            <el-button :icon="'Refresh'" @click="store.refreshCurrent()">刷新</el-button>
            <el-button :icon="'MagicStick'" type="primary" plain @click="openReplan">重新规划</el-button>
            <el-button :icon="'Document'" @click="store.downloadReport('md')">导出 Markdown</el-button>
            <el-button :icon="'Download'" @click="store.downloadReport('json')">导出 JSON</el-button>
          </div>
        </div>
        <p class="ip-requirement">{{ plan.rawRequirement }}</p>
        <div class="ip-meta">
          <span>计划 ID：<span class="ip-mono">{{ plan.planId }}</span></span>
          <el-divider direction="vertical" />
          <span>迭代窗口：{{ plan.iterationStartDate }} ~ {{ plan.iterationDeadline }}</span>
          <el-divider direction="vertical" />
          <span>可用开发：{{ plan.devMemberCount }} 人</span>
          <el-divider direction="vertical" />
          <span>关键路径工期：{{ plan.totalDurationHours }}h（{{ hoursToWorkDays(plan.totalDurationHours) }}）</span>
        </div>
      </div>

      <!-- KPI -->
      <KpiCards :plan="plan" />

      <!-- 里程碑节点（自动标记） -->
      <div v-if="milestones.length > 0" class="ip-panel">
        <div class="ip-panel__title">
          <span>里程碑节点（自动标记）</span>
          <el-tag size="small" effect="plain">共 {{ milestones.length }} 个</el-tag>
        </div>
        <div class="ip-milestones">
          <el-tag
            v-for="item in milestones"
            :key="item.taskId"
            size="large"
            effect="plain"
            :type="item.kind === 'RELEASE' ? 'danger' : 'primary'"
            class="ip-milestone"
            @click="locateTask(item.taskId)"
          >
            {{ item.kind === 'RELEASE' ? '发布里程碑' : '交付里程碑' }} · {{ item.taskName }} · {{ item.date }}
          </el-tag>
        </div>
        <p class="ip-muted" style="margin: 8px 0 0; font-size: 12px">
          里程碑由任务类型（部署）与依赖末端自动推断，仅用于展示；点击标签可定位到任务管理页。
        </p>
      </div>

      <!-- 关键路径 + 风险 -->
      <div class="ip-grid ip-grid--2">
        <div class="ip-panel">
          <div class="ip-panel__title">
            <span>关键路径（{{ criticalTasks.length }} 项，slack = 0）</span>
          </div>
          <el-empty v-if="criticalTasks.length === 0" description="未识别到关键路径" :image-size="70" />
          <div v-else class="ip-timeline">
            <div v-for="task in criticalTasks" :key="task.taskId" class="ip-timeline__item">
              <div class="ip-timeline__dot" :style="{ background: TASK_TYPE_COLOR[task.taskType] }" />
              <div class="ip-timeline__body">
                <div class="ip-timeline__name">{{ task.taskName }}</div>
                <div class="ip-timeline__meta">
                  <el-tag size="small" effect="plain" :type="'info'">{{ TASK_TYPE_LABEL[task.taskType] }}</el-tag>
                  <span class="ip-muted">es {{ task.es }}h · ls {{ task.ls }}h · {{ task.estimatedHours }}h</span>
                  <span v-if="task.assignee" class="ip-muted">· {{ task.assignee }}</span>
                </div>
                <el-progress
                  :percentage="Math.round(((task.es + task.estimatedHours) / maxEs) * 100)"
                  :show-text="false"
                  :stroke-width="6"
                  status="exception"
                />
              </div>
            </div>
          </div>
        </div>

        <div class="ip-panel">
          <div class="ip-panel__title">
            <span>风险清单</span>
            <el-tag size="small" :type="store.riskStats.high > 0 ? 'danger' : 'success'" effect="plain">
              共 {{ store.riskStats.total }} 条
            </el-tag>
          </div>
          <RiskPanel :risks="plan.riskList" :task-name-of="taskNameOf" @locate="locateTask" />
        </div>
      </div>

      <!-- 燃尽图 -->
      <div class="ip-panel">
        <div class="ip-panel__title"><span>燃尽趋势</span></div>
        <BurndownChart :data="plan.burndownRawData" :total-hours="plan.totalEstimatedHours" />
      </div>

      <!-- 迭代报表预览 -->
      <div class="ip-panel">
        <div class="ip-panel__title">
          <span>迭代报表（Markdown）</span>
          <el-button link type="primary" :icon="'CopyDocument'" @click="store.downloadReport('md')">下载</el-button>
        </div>
        <pre class="ip-markdown">{{ store.report?.content ?? '暂无报表内容' }}</pre>
      </div>

      <div class="ip-muted ip-updated">最后更新：{{ lastUpdated }}</div>
    </template>

    <!-- 重规划进度：已改为异步作业，弹窗关掉后必须留一条可见的「还在跑 + 可取消」的线 -->
    <div v-if="store.writeJob" class="ip-job">
      <div class="ip-job__head">
        <span class="ip-job__title">{{ store.writeJob.label }}</span>
        <el-tag size="small" effect="plain" type="warning">{{ jobStatusText }}</el-tag>
      </div>
      <el-progress :percentage="jobPercent" :status="jobProgressStatus" :stroke-width="6" />
      <p class="ip-job__message">{{ store.writeJob.message }}</p>
      <div class="ip-job__foot">
        <el-button size="small" type="danger" plain :disabled="!cancellable" @click="store.cancelWrite()">
          取消
        </el-button>
      </div>
    </div>

    <!-- 重规划弹窗 -->
    <el-dialog v-model="replanVisible" title="重新规划" width="480px">
      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="重规划将重跑环检测、AOE 排期与风险扫描，任务内容保持不变。"
        style="margin-bottom: 14px"
      />
      <el-form label-width="100px">
        <el-form-item label="迭代起始">
          <el-date-picker
            v-model="replanForm.iterationStartDate"
            type="date"
            value-format="YYYY-MM-DD"
            style="width: 100%"
          />
        </el-form-item>
        <el-form-item label="迭代截止">
          <el-date-picker
            v-model="replanForm.iterationDeadline"
            type="date"
            value-format="YYYY-MM-DD"
            style="width: 100%"
          />
        </el-form-item>
        <el-form-item label="开发人数">
          <el-input-number v-model="replanForm.devMemberCount" :min="1" :max="50" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="replanVisible = false">取消</el-button>
        <el-button type="primary" :loading="store.mutating" @click="submitReplan">执行重规划</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.ip-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.ip-requirement {
  margin: 0 0 12px;
  line-height: 1.8;
  font-size: 13.5px;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--ip-text);
}

.ip-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  font-size: 12.5px;
  color: var(--ip-text-secondary);
}

.ip-timeline {
  display: flex;
  flex-direction: column;
  gap: 14px;
  max-height: 420px;
  overflow: auto;
  padding-right: 4px;
}

.ip-timeline__item {
  display: flex;
  gap: 10px;
}

.ip-timeline__dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  margin-top: 6px;
  flex: 0 0 auto;
}

.ip-timeline__body {
  flex: 1;
  min-width: 0;
}

.ip-timeline__name {
  font-size: 13.5px;
  font-weight: 600;
  margin-bottom: 4px;
}

.ip-timeline__meta {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  margin-bottom: 6px;
  flex-wrap: wrap;
}

.ip-updated {
  font-size: 12px;
  text-align: right;
}

.ip-milestones {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.ip-milestone {
  cursor: pointer;
}
</style>
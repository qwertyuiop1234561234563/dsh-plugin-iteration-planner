<script setup lang="ts">
/**
 * 任务日历：以「周网格」呈现迭代窗口内的任务排期与里程碑节点。
 *
 * ⚠️ 定位说明：
 *  - 排期日期由 `utils/analytics.taskDateRange` 按**工作日**从任务最早开始时间（`es`）
 *    与预估工时换算得到，仅用于展示；真正的排期口径以引擎 `src/algorithm/**` 为准；
 *  - 里程碑为展示性推断（发布任务 / 末端交付任务），不参与排期与风险判定；
 *  - 本页只读，不产生任何业务写入。
 */
import { computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import type { ProjectPlan, Task } from '@/api/types'
import { usePlanStore } from '@/stores/plan'
import { detectMilestones, taskDateRange } from '@/utils/analytics'
import { STATUS_COLOR, STATUS_LABEL, TASK_TYPE_LABEL, truncate } from '@/utils/format'
import { addDays, formatISODate, isWeekend, parseISODate } from '@/utils/workday'

const store = usePlanStore()
const route = useRoute()
const router = useRouter()

const plan = computed<ProjectPlan | null>(() => store.plan)

/** 周一为一周之首 */
const WEEK_LABELS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']

/** 里程碑（taskId → 里程碑信息） */
const milestoneMap = computed(() => {
  const map = new Map<string, { kind: 'RELEASE' | 'DELIVERY'; date: string }>()
  for (const item of detectMilestones(plan.value)) map.set(item.taskId, { kind: item.kind, date: item.date })
  return map
})

/** 每个任务的排期区间（taskId → { start, end }） */
const taskRanges = computed(() => {
  const current = plan.value
  const map = new Map<string, { start: string; end: string }>()
  if (!current) return map
  for (const task of current.taskList) map.set(task.taskId, taskDateRange(current, task))
  return map
})

/** 日历覆盖区间：迭代起点所在周的周一 → 最晚节点所在周的周日 */
const range = computed(() => {
  const current = plan.value
  if (!current) return null
  let last = current.iterationDeadline
  for (const task of current.taskList) {
    const end = taskRanges.value.get(task.taskId)?.end
    if (end && end > last) last = end
  }
  if (store.plannedEndDate && store.plannedEndDate > last) last = store.plannedEndDate

  const start = parseISODate(current.iterationStartDate)
  // 回退到周一
  const weekday = (start.getUTCDay() + 6) % 7
  const gridStart = addDays(start, -weekday)
  const endDate = parseISODate(last)
  const endWeekday = (endDate.getUTCDay() + 6) % 7
  const gridEnd = addDays(endDate, 6 - endWeekday)
  return { start: gridStart, end: gridEnd }
})

/** 单元格模型 */
interface DayCell {
  iso: string
  date: Date
  weekend: boolean
  today: boolean
  inIteration: boolean
  tasks: Task[]
  hours: number
  milestones: Array<{ taskId: string; taskName: string; kind: 'RELEASE' | 'DELIVERY' }>
}

/** 按周分行的日历网格 */
const weeks = computed<DayCell[][]>(() => {
  const current = plan.value
  const span = range.value
  if (!current || !span) return []
  const todayISO = formatISODate(new Date())
  const rows: DayCell[][] = []
  let cursor = span.start
  let row: DayCell[] = []

  while (cursor.getTime() <= span.end.getTime()) {
    const iso = formatISODate(cursor)
    const weekend = isWeekend(cursor)
    const tasks = weekend
      ? []
      : current.taskList.filter((task) => {
          const item = taskRanges.value.get(task.taskId)
          return !!item && item.start <= iso && iso <= item.end
        })
    const hours = tasks.reduce((sum, task) => sum + task.estimatedHours, 0)
    const milestones = tasks
      .filter((task) => milestoneMap.value.get(task.taskId)?.date === iso)
      .map((task) => ({
        taskId: task.taskId,
        taskName: task.taskName,
        kind: milestoneMap.value.get(task.taskId)!.kind,
      }))

    row.push({
      iso,
      date: cursor,
      weekend,
      today: iso === todayISO,
      inIteration: current.iterationStartDate <= iso && iso <= current.iterationDeadline,
      tasks,
      hours,
      milestones,
    })

    if (row.length === 7) {
      rows.push(row)
      row = []
    }
    cursor = addDays(cursor, 1)
  }
  if (row.length > 0) rows.push(row)
  return rows
})

/** 仅列出被识别为里程碑的任务（右侧清单） */
const milestoneList = computed(() => detectMilestones(plan.value))

/** 有任务排布的单元格最大工时（用于负载浓淡） */
const maxCellHours = computed(() => {
  let max = 0
  for (const week of weeks.value) for (const cell of week) max = Math.max(max, cell.hours)
  return max
})

/** 单元格背景浓淡：按当日排布工时相对最大值 */
function cellTint(cell: DayCell): string | undefined {
  if (cell.hours <= 0 || maxCellHours.value <= 0) return undefined
  const ratio = cell.hours / maxCellHours.value
  return `rgba(14, 107, 168, ${(0.06 + ratio * 0.16).toFixed(3)})`
}

/** 点击任务 → 跳转任务管理页并按名称检索 */
function openTask(task: Task): void {
  void router.push({ name: 'plan-tasks', params: { planId: plan.value?.planId }, query: { keyword: task.taskName } })
}

onMounted(async () => {
  const planId = route.params.planId as string | undefined
  if (planId && store.currentPlanId !== planId) await store.loadPlan(planId)
})
</script>

<template>
  <div class="ip-page">
    <el-empty v-if="!plan" description="请选择或新建一个迭代计划" />

    <template v-else>
      <div class="ip-panel">
        <div class="ip-panel__title">
          <span>任务日历</span>
          <span class="ip-muted" style="font-size: 12px">
            迭代窗口 {{ plan.iterationStartDate }} ~ {{ plan.iterationDeadline }} · 单元格底色越深表示当日排布工时越多
          </span>
        </div>

        <div class="ip-calendar">
          <div class="ip-calendar__head">
            <span v-for="label in WEEK_LABELS" :key="label" :class="{ 'is-weekend': label === '周六' || label === '周日' }">
              {{ label }}
            </span>
          </div>

          <div v-for="(week, weekIndex) in weeks" :key="weekIndex" class="ip-calendar__week">
            <div
              v-for="cell in week"
              :key="cell.iso"
              class="ip-calendar__cell"
              :class="{ 'is-weekend': cell.weekend, 'is-outside': !cell.inIteration, 'is-today': cell.today }"
              :style="{ background: cellTint(cell) }"
            >
              <div class="ip-calendar__date">
                <span>{{ cell.date.getUTCMonth() + 1 }}/{{ cell.date.getUTCDate() }}</span>
                <span v-if="cell.milestones.length > 0" class="ip-calendar__flag">
                  <el-icon><Flag /></el-icon>
                </span>
              </div>

              <div v-if="cell.hours > 0" class="ip-calendar__load">{{ cell.hours }}h</div>

              <button
                v-for="task in cell.tasks"
                :key="task.taskId"
                type="button"
                class="ip-calendar__task"
                :class="{ 'is-critical': store.criticalSet.has(task.taskId) }"
                :style="{ borderLeftColor: STATUS_COLOR[task.status] }"
                :title="`${task.taskName} · ${TASK_TYPE_LABEL[task.taskType]} · ${STATUS_LABEL[task.status]} · ${task.estimatedHours}h`"
                @click="openTask(task)"
              >
                <span class="ip-calendar__task-name">{{ truncate(task.taskName, 14) }}</span>
                <span
                  v-if="milestoneMap.get(task.taskId)"
                  class="ip-calendar__task-tag"
                  :class="milestoneMap.get(task.taskId)!.kind === 'RELEASE' ? 'is-release' : 'is-delivery'"
                >
                  {{ milestoneMap.get(task.taskId)!.kind === 'RELEASE' ? '发布' : '交付' }}
                </span>
              </button>
            </div>
          </div>
        </div>
      </div>

      <div class="ip-grid ip-grid--2">
        <div class="ip-panel">
          <div class="ip-panel__title">
            <span>里程碑节点</span>
            <el-tag size="small" effect="plain">共 {{ milestoneList.length }} 个</el-tag>
          </div>
          <el-table v-if="milestoneList.length > 0" :data="milestoneList" size="small" stripe>
            <el-table-column label="类型" width="110">
              <template #default="{ row }">
                <el-tag :type="row.kind === 'RELEASE' ? 'danger' : 'primary'" size="small" effect="plain">
                  {{ row.kind === 'RELEASE' ? '发布里程碑' : '交付里程碑' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="任务" prop="taskName" min-width="200" show-overflow-tooltip />
            <el-table-column label="计划日期" prop="date" width="120" />
          </el-table>
          <el-empty v-else description="当前计划未识别出里程碑任务" :image-size="60" />
        </div>

        <div class="ip-panel">
          <div class="ip-panel__title"><span>读图说明</span></div>
          <ul class="ip-calendar__notes">
            <li>任务条左侧色条对应该任务当前状态（待办 / 进行中 / 阻塞 / 测试中 / 已完成 / 已延期）。</li>
            <li>带<strong>加粗左边框</strong>的任务为引擎判定的关键路径任务（松弛时间为 0）。</li>
            <li>排期日期按工作日推进（周一~周五，单开发人单日 8h）换算，仅用于展示；权威口径以引擎为准。</li>
            <li>点击任务条可跳转「任务管理」页并按任务名称过滤。</li>
          </ul>
          <div class="ip-calendar__legend">
            <span><i :style="{ background: STATUS_COLOR.TODO }" />待办</span>
            <span><i :style="{ background: STATUS_COLOR.IN_PROGRESS }" />进行中</span>
            <span><i :style="{ background: STATUS_COLOR.BLOCKED }" />阻塞</span>
            <span><i :style="{ background: STATUS_COLOR.TESTING }" />测试中</span>
            <span><i :style="{ background: STATUS_COLOR.DONE }" />已完成</span>
            <span><i :style="{ background: STATUS_COLOR.DELAYED }" />已延期</span>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.ip-calendar {
  display: grid;
  gap: 4px;
}

.ip-calendar__head,
.ip-calendar__week {
  display: grid;
  grid-template-columns: repeat(7, minmax(0, 1fr));
  gap: 4px;
}

.ip-calendar__head span {
  padding: 4px 6px;
  font-family: var(--ip-font-mono);
  font-size: 11px;
  letter-spacing: 0.08em;
  color: var(--ip-text-muted);
  text-align: center;
}

.ip-calendar__head span.is-weekend {
  color: var(--ip-text-secondary);
  opacity: 0.7;
}

.ip-calendar__cell {
  min-height: 92px;
  padding: 5px 6px;
  border: 1px solid var(--ip-border);
  border-radius: var(--ip-radius);
  background: var(--ip-surface);
  display: flex;
  flex-direction: column;
  gap: 3px;
  overflow: hidden;
}

.ip-calendar__cell.is-weekend {
  background: var(--ip-surface-sunk);
}

.ip-calendar__cell.is-outside {
  opacity: 0.5;
}

.ip-calendar__cell.is-today {
  border-color: var(--ip-brand);
  box-shadow: inset 0 0 0 1px var(--ip-brand);
}

.ip-calendar__date {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-family: var(--ip-font-mono);
  font-size: 11px;
  font-weight: 600;
  color: var(--ip-text-secondary);
}

.ip-calendar__flag {
  color: var(--ip-danger);
  display: inline-flex;
}

.ip-calendar__load {
  font-family: var(--ip-font-mono);
  font-size: 10px;
  color: var(--ip-text-muted);
}

.ip-calendar__task {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 4px;
  width: 100%;
  padding: 2px 5px;
  border: none;
  border-left: 3px solid transparent;
  border-radius: 2px;
  background: var(--ip-surface);
  box-shadow: 0 0 0 1px var(--ip-border) inset;
  font-size: 11px;
  color: var(--ip-text);
  cursor: pointer;
  text-align: left;
  transition: background 0.15s ease;
}

.ip-calendar__cell .ip-calendar__task {
  background: color-mix(in srgb, var(--ip-surface) 88%, var(--ip-brand) 12%);
}

.ip-calendar__task:hover {
  filter: brightness(1.06);
}

.ip-calendar__task.is-critical {
  border-left-width: 3px;
  box-shadow: 0 0 0 1px var(--ip-brand) inset;
}

.ip-calendar__task-name {
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}

.ip-calendar__task-tag {
  flex-shrink: 0;
  padding: 0 3px;
  border-radius: 2px;
  font-family: var(--ip-font-mono);
  font-size: 9px;
  color: #fff;
}

.ip-calendar__task-tag.is-release {
  background: var(--ip-danger);
}

.ip-calendar__task-tag.is-delivery {
  background: var(--ip-brand);
}

.ip-calendar__notes {
  margin: 0;
  padding-left: 18px;
  display: grid;
  gap: 6px;
  font-size: 12.5px;
  color: var(--ip-text-secondary);
  line-height: 1.7;
}

.ip-calendar__legend {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-top: 12px;
  font-size: 11.5px;
  color: var(--ip-text-secondary);
}

.ip-calendar__legend span {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.ip-calendar__legend i {
  display: inline-block;
  width: 12px;
  height: 10px;
  border-radius: 2px;
}
</style>

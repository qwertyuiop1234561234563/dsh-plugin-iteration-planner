<script setup lang="ts">
/**
 * 甘特图页：以「工作日列」呈现任务排期（es / 工期 / slack）。
 *
 * 数据说明：横轴的起点、时长与每任务的 es / ls / slack 全部来自引擎的 AOE 计算结果
 * （`src/algorithm/aoe.ts`），前端只做「小时 → 像素」的展示换算；工作日的日期列由
 * `@/utils/workday`（展示层镜像）生成，不参与任何业务判定。
 *
 * 视觉约定：
 *  - 红色实心条 = 关键路径任务（slack = 0）；
 *  - 非关键任务按责任人着色（同一责任人颜色一致，便于识别资源分布）；
 *  - 浅色虚影 = 松弛浮动区间（slack），表示该任务可推迟的余量。
 */
import { computed, ref } from 'vue'
import type { ProjectPlan, Task } from '@/api/types'
import { usePlanStore } from '@/stores/plan'
import { STATUS_LABEL, TASK_TYPE_LABEL } from '@/utils/format'
import { ASSIGNEE_PALETTE, TOKEN } from '@/utils/palette'
import { WORK_HOURS_PER_DAY, enumerateWorkdays, takeWorkdays } from '@/utils/workday'

const store = usePlanStore()
const plan = computed<ProjectPlan | null>(() => store.plan)

/** 单列工作日宽度（px）与单行高度（px），名称列与时间轴共用行高以保证对齐 */
const COL_WIDTH = 64
const ROW_HEIGHT = 40
const HEAD_HEIGHT = 34

const criticalOnly = ref(false)
const showFloat = ref(true)
const keyword = ref('')

/** 时间轴覆盖的工作日列：以总工期为主，若截止日期更晚则延伸到截止日期 */
const days = computed(() => {
  const current = plan.value
  if (!current) return []
  const needed = Math.max(1, Math.ceil(current.totalDurationHours / WORK_HOURS_PER_DAY))
  const deadlineSpan = enumerateWorkdays(current.iterationStartDate, current.iterationDeadline).length
  return takeWorkdays(current.iterationStartDate, Math.max(needed, deadlineSpan, 1))
})

/** 今天（本地日历日，仅用于时间轴标记；与业务排期无关） */
const today = computed(() => {
  const now = new Date()
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`
})

const chartWidth = computed(() => days.value.length * COL_WIDTH)

/** 截止日期标记位置（不在覆盖范围内则为 null） */
const deadlineLeft = computed(() => {
  const index = days.value.indexOf(plan.value?.iterationDeadline ?? '')
  return index >= 0 ? (index + 1) * COL_WIDTH : null
})

/** 今天标记位置 */
const todayLeft = computed(() => {
  const index = days.value.indexOf(today.value)
  return index >= 0 ? index * COL_WIDTH : null
})

/** 责任人 → 颜色（按出现顺序分配调色板，保证同一责任人颜色稳定） */
const assigneeColor = computed(() => {
  const map = new Map<string, string>()
  let cursor = 0
  for (const task of plan.value?.taskList ?? []) {
    const name = task.assignee || '待分配'
    if (!map.has(name)) {
      map.set(name, ASSIGNEE_PALETTE[cursor % ASSIGNEE_PALETTE.length])
      cursor += 1
    }
  }
  return map
})

/** 图表行：过滤 + 几何换算（小时 → 像素） */
const rows = computed(() => {
  const current = plan.value
  if (!current) return []
  const lowered = keyword.value.trim().toLowerCase()
  return current.taskList
    .filter((task: Task) => {
      if (criticalOnly.value && task.slack !== 0) return false
      if (!lowered) return true
      return (
        task.taskName.toLowerCase().includes(lowered) ||
        (task.assignee ?? '').toLowerCase().includes(lowered) ||
        TASK_TYPE_LABEL[task.taskType].includes(lowered)
      )
    })
    .map((task: Task) => ({
      task,
      left: (task.es / WORK_HOURS_PER_DAY) * COL_WIDTH,
      width: Math.max((task.estimatedHours / WORK_HOURS_PER_DAY) * COL_WIDTH, 8),
      floatLeft: ((task.es + task.estimatedHours) / WORK_HOURS_PER_DAY) * COL_WIDTH,
      floatWidth: Math.max((task.slack / WORK_HOURS_PER_DAY) * COL_WIDTH, 0),
      color: assigneeColor.value.get(task.assignee || '待分配') ?? TOKEN.muted,
      critical: task.slack === 0,
    }))
    .sort((a, b) => a.task.es - b.task.es || a.task.taskId.localeCompare(b.task.taskId))
})

/** 图例：责任人与颜色 */
const legend = computed(() => [...assigneeColor.value.entries()].map(([name, color]) => ({ name, color })))
</script>

<template>
  <div class="ip-page">
    <el-empty v-if="!plan" description="请选择或新建一个迭代计划" />

    <template v-else>
      <div class="ip-panel">
        <div class="ip-panel__title">
          <span>排期甘特图</span>
          <div class="ip-gantt-actions">
            <el-input
              v-model="keyword"
              :prefix-icon="'Search'"
              placeholder="按任务 / 责任人 / 类型过滤"
              clearable
              style="width: 220px"
            />
            <el-checkbox v-model="criticalOnly">仅关键路径</el-checkbox>
            <el-checkbox v-model="showFloat">显示松弛浮动</el-checkbox>
            <el-button :icon="'Refresh'" @click="store.refreshCurrent()">刷新</el-button>
          </div>
        </div>

        <div class="ip-gantt-meta">
          <span>迭代窗口：{{ plan.iterationStartDate }} ~ {{ plan.iterationDeadline }}</span>
          <el-divider direction="vertical" />
          <span>计划完成：{{ store.plannedEndDate ?? '—' }}</span>
          <el-divider direction="vertical" />
          <span>关键路径工期：{{ plan.totalDurationHours }}h（{{ store.durationWorkDays }} 个工作日）</span>
          <el-divider direction="vertical" />
          <span>展示 {{ rows.length }} / {{ plan.taskList.length }} 项</span>
        </div>

        <div class="ip-gantt">
          <!-- 左侧固定：任务名称列 -->
          <div class="ip-gantt__names">
            <div class="ip-gantt__names-head" :style="{ height: `${HEAD_HEIGHT}px` }">任务（{{ rows.length }}）</div>
            <div
              v-for="row in rows"
              :key="row.task.taskId"
              class="ip-gantt__name-row"
              :class="{ 'is-critical': row.critical }"
              :style="{ height: `${ROW_HEIGHT}px` }"
            >
              <div class="ip-gantt__name">
                <span class="ip-gantt__name-text" :title="row.task.taskName">{{ row.task.taskName }}</span>
                <el-tag v-if="row.critical" size="small" type="danger" effect="plain">关键</el-tag>
              </div>
              <div class="ip-gantt__name-meta ip-muted">
                {{ TASK_TYPE_LABEL[row.task.taskType] }} · {{ row.task.estimatedHours }}h ·
                {{ STATUS_LABEL[row.task.status] }} · {{ row.task.assignee || '待分配' }}
              </div>
            </div>
          </div>

          <!-- 右侧：可横向滚动的时间轴 -->
          <div class="ip-gantt__chart ip-scroll">
            <div class="ip-gantt__inner" :style="{ width: `${chartWidth}px` }">
              <div class="ip-gantt__axis" :style="{ height: `${HEAD_HEIGHT}px` }">
                <div
                  v-for="day in days"
                  :key="day"
                  class="ip-gantt__tick"
                  :class="{ 'is-today': day === today }"
                  :style="{ width: `${COL_WIDTH}px` }"
                >
                  {{ day.slice(5) }}
                </div>
              </div>

              <div
                v-for="row in rows"
                :key="row.task.taskId"
                class="ip-gantt__track"
                :style="{ height: `${ROW_HEIGHT}px` }"
              >
                <div
                  v-for="day in days"
                  :key="day"
                  class="ip-gantt__grid-col"
                  :class="{ 'is-today': day === today }"
                  :style="{ width: `${COL_WIDTH}px` }"
                />
                <div
                  v-if="showFloat && row.floatWidth > 4"
                  class="ip-gantt__float"
                  :style="{ left: `${row.floatLeft}px`, width: `${row.floatWidth}px` }"
                  :title="`松弛 ${row.task.slack}h，最晚开始 ${row.task.ls}h`"
                />
                <div
                  class="ip-gantt__bar"
                  :class="{ 'is-critical': row.critical }"
                  :style="{
                    left: `${row.left}px`,
                    width: `${row.width}px`,
                    background: row.critical ? undefined : row.color,
                  }"
                  :title="`${row.task.taskName}｜es ${row.task.es}h / ls ${row.task.ls}h / slack ${row.task.slack}h`"
                >
                  <span v-if="row.width > 56" class="ip-gantt__bar-label">{{ row.task.estimatedHours }}h</span>
                </div>
              </div>

              <div v-if="todayLeft !== null" class="ip-gantt__marker ip-gantt__marker--today" :style="{ left: `${todayLeft}px` }">
                <span class="ip-gantt__marker-label">今天</span>
              </div>
              <div v-if="deadlineLeft !== null" class="ip-gantt__marker ip-gantt__marker--deadline" :style="{ left: `${deadlineLeft}px` }">
                <span class="ip-gantt__marker-label">截止</span>
              </div>
            </div>
          </div>
        </div>

        <div class="ip-gantt-legend">
          <span class="ip-gantt-legend__item"><i class="ip-gantt-legend__swatch" style="background: var(--ip-danger)" />关键路径</span>
          <span v-for="item in legend" :key="item.name" class="ip-gantt-legend__item">
            <i class="ip-gantt-legend__swatch" :style="{ background: item.color }" />{{ item.name }}
          </span>
          <span class="ip-gantt-legend__item"><i class="ip-gantt-legend__swatch ip-gantt-legend__swatch--float" />松弛浮动</span>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.ip-gantt-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.ip-gantt-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  font-size: 12.5px;
  color: var(--ip-text-secondary);
  margin-bottom: 12px;
}

.ip-gantt {
  display: flex;
  border: 1px solid var(--ip-border);
  border-radius: 8px;
  overflow: hidden;
  max-height: 620px;
  overflow-y: auto;
}

.ip-gantt__names {
  flex: 0 0 268px;
  border-right: 1px solid var(--ip-border);
  background: #fafbfc;
}

.ip-gantt__names-head {
  display: flex;
  align-items: center;
  padding: 0 12px;
  font-size: 12px;
  font-weight: 600;
  color: var(--ip-text-secondary);
  border-bottom: 1px solid var(--ip-border);
  background: #f2f4f8;
  position: sticky;
  top: 0;
  z-index: 3;
}

.ip-gantt__name-row {
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 2px;
  padding: 0 12px;
  border-bottom: 1px solid #f1f3f7;
  overflow: hidden;
}

.ip-gantt__name-row.is-critical {
  box-shadow: inset 3px 0 0 var(--ip-danger);
}

.ip-gantt__name {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.ip-gantt__name-text {
  font-size: 13px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.ip-gantt__name-meta {
  font-size: 11px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.ip-gantt__chart {
  flex: 1;
  min-width: 0;
}

.ip-gantt__inner {
  position: relative;
}

.ip-gantt__axis {
  display: flex;
  border-bottom: 1px solid var(--ip-border);
  background: #f2f4f8;
  position: sticky;
  top: 0;
  z-index: 2;
}

.ip-gantt__tick {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  color: var(--ip-text-secondary);
  border-right: 1px solid #e9edf3;
  box-sizing: border-box;
}

.ip-gantt__tick.is-today {
  background: rgba(36, 104, 242, 0.1);
  color: var(--ip-brand);
  font-weight: 600;
}

.ip-gantt__track {
  position: relative;
  display: flex;
  border-bottom: 1px solid #f1f3f7;
}

.ip-gantt__grid-col {
  flex: 0 0 auto;
  border-right: 1px solid #f4f6fa;
  box-sizing: border-box;
}

.ip-gantt__grid-col.is-today {
  background: rgba(36, 104, 242, 0.05);
}

.ip-gantt__float {
  position: absolute;
  top: 50%;
  transform: translateY(-50%);
  height: 8px;
  border-radius: 4px;
  background: repeating-linear-gradient(45deg, #c8d3e6, #c8d3e6 4px, #e4eaf3 4px, #e4eaf3 8px);
  opacity: 0.9;
}

.ip-gantt__bar {
  position: absolute;
  top: 50%;
  transform: translateY(-50%);
  height: 20px;
  border-radius: 5px;
  background: var(--ip-brand);
  display: flex;
  align-items: center;
  justify-content: flex-end;
  padding: 0 6px;
  box-shadow: 0 1px 3px rgba(31, 45, 61, 0.18);
  cursor: default;
}

.ip-gantt__bar.is-critical {
  background: var(--ip-danger);
}

.ip-gantt__bar-label {
  font-size: 11px;
  color: #fff;
  font-weight: 600;
}

.ip-gantt__marker {
  position: absolute;
  top: 0;
  bottom: 0;
  width: 0;
  border-left: 1.5px dashed var(--ip-danger);
  z-index: 1;
  pointer-events: none;
}

.ip-gantt__marker--today {
  border-left-color: var(--ip-brand);
}

.ip-gantt__marker-label {
  position: absolute;
  top: 3px;
  left: 2px;
  font-size: 10px;
  color: #fff;
  background: var(--ip-danger);
  padding: 1px 4px;
  border-radius: 3px;
  white-space: nowrap;
}

.ip-gantt__marker--today .ip-gantt__marker-label {
  background: var(--ip-brand);
}

.ip-gantt-legend {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 16px;
  margin-top: 12px;
  font-size: 12px;
  color: var(--ip-text-secondary);
}

.ip-gantt-legend__item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.ip-gantt-legend__swatch {
  width: 14px;
  height: 10px;
  border-radius: 3px;
  display: inline-block;
}

.ip-gantt-legend__swatch--float {
  background: repeating-linear-gradient(45deg, #c8d3e6, #c8d3e6 3px, #e4eaf3 3px, #e4eaf3 6px);
}
</style>
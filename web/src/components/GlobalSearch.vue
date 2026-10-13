<script setup lang="ts">
/**
 * 全局搜索（命令面板，Ctrl/Cmd + K）。
 *
 * 搜索对象：
 *  - 页面导航：直接由路由表派生（`meta.sheet` + `meta.title`），因此新增页面自动进面板；
 *  - 迭代计划：来自计划摘要列表；
 *  - 当前计划的任务：命中后跳转任务管理页并自动按其任务名筛选（与高级筛选器联动）。
 *
 * 键盘：↑/↓ 移动、Enter 打开、Esc 关闭。
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import type { InputInstance } from 'element-plus'
import { usePlanStore } from '@/stores/plan'
import { truncate } from '@/utils/format'

const visible = defineModel<boolean>({ default: false })

const store = usePlanStore()
const router = useRouter()
const inputRef = ref<InputInstance>()
const keyword = ref('')
const activeIndex = ref(0)

/** 搜索结果条目 */
interface SearchItem {
  key: string
  /** 类别：页面 / 计划 / 任务 */
  kind: 'nav' | 'plan' | 'task'
  label: string
  hint: string
  sheet?: string
  disabled?: boolean
  run: () => void
}

/** 当前计划 ID（既可从路由参数取，也可从 store 取，取其一即可） */
const planId = computed(() => store.currentPlanId)

/** 页面导航命令：由路由表派生，避免与左侧菜单重复维护 */
const navItems = computed<SearchItem[]>(() => {
  return router
    .getRoutes()
    .filter((record) => typeof record.meta?.sheet === 'string' && typeof record.meta?.title === 'string' && !record.redirect)
    .map((record) => {
      const needsPlan = record.path.includes(':planId')
      const sheet = String(record.meta.sheet)
      return {
        key: `nav:${String(record.name)}`,
        kind: 'nav' as const,
        label: String(record.meta.title),
        hint: needsPlan ? '当前计划视图' : '全局页面',
        sheet,
        disabled: needsPlan && !planId.value,
        run: () => {
          const target = needsPlan ? { name: record.name, params: { planId: planId.value } } : { name: record.name, params: {} }
          void router.push(target)
        },
      }
    })
})

/** 计划条目 */
const planItems = computed<SearchItem[]>(() =>
  store.summaries.map((summary) => ({
    key: `plan:${summary.planId}`,
    kind: 'plan' as const,
    label: truncate(summary.rawRequirement, 46),
    hint: `${summary.taskCount} 任务 · ${summary.totalDurationHours}h · ${summary.iterationStartDate} ~ ${summary.iterationDeadline}`,
    sheet: '计划',
    run: () => void router.push({ name: 'plan-overview', params: { planId: summary.planId } }),
  })),
)

/** 当前计划的任务条目 */
const taskItems = computed<SearchItem[]>(() => {
  const current = store.plan
  if (!current || !planId.value) return []
  return current.taskList.map((task) => ({
    key: `task:${task.taskId}`,
    kind: 'task' as const,
    label: task.taskName,
    hint: `${task.taskType} · ${task.priority} · ${task.estimatedHours}h · ${task.assignee ?? '待分配'}`,
    sheet: '任务',
    run: () =>
      void router.push({ name: 'plan-tasks', params: { planId: planId.value }, query: { keyword: task.taskName } }),
  }))
})

/** 过滤后的结果（匹配评分：完全前缀 > 包含） */
const results = computed<SearchItem[]>(() => {
  const text = keyword.value.trim().toLowerCase()
  const pool = [...navItems.value, ...planItems.value, ...taskItems.value]
  if (!text) return pool
  const scored: Array<{ item: SearchItem; score: number }> = []
  for (const item of pool) {
    const label = item.label.toLowerCase()
    const haystack = `${label} ${item.hint.toLowerCase()}`
    if (label.startsWith(text)) scored.push({ item, score: 3 })
    else if (label.includes(text)) scored.push({ item, score: 2 })
    else if (haystack.includes(text)) scored.push({ item, score: 1 })
  }
  return scored.sort((a, b) => b.score - a.score).map((entry) => entry.item)
})

/** 键盘高亮时同步滚动到可视区域 */
const listRef = ref<HTMLElement>()

watch(results, () => {
  activeIndex.value = 0
})

watch(visible, (open) => {
  if (open) {
    keyword.value = ''
    activeIndex.value = 0
    // 键盘事件挂到 window：即使输入框尚未聚焦，↑/↓/Enter/Esc 也能生效
    window.addEventListener('keydown', onKeydown)
    focusInput()
  } else {
    window.removeEventListener('keydown', onKeydown)
  }
})

onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))

/** 弹层打开后聚焦输入框（仅靠 nextTick 时对话框可能尚未挂载，故重试一轮） */
function focusInput(): void {
  void nextTick(() => {
    if (inputRef.value) {
      inputRef.value.focus()
      return
    }
    void nextTick(() => inputRef.value?.focus())
  })
}

function select(item: SearchItem | undefined): void {
  if (!item || item.disabled) return
  item.run()
  visible.value = false
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'ArrowDown') {
    event.preventDefault()
    if (results.value.length === 0) return
    activeIndex.value = (activeIndex.value + 1) % results.value.length
    scrollActiveIntoView()
  } else if (event.key === 'ArrowUp') {
    event.preventDefault()
    if (results.value.length === 0) return
    activeIndex.value = (activeIndex.value - 1 + results.value.length) % results.value.length
    scrollActiveIntoView()
  } else if (event.key === 'Enter') {
    event.preventDefault()
    select(results.value[activeIndex.value])
  } else if (event.key === 'Escape') {
    visible.value = false
  }
}

/** 让高亮项保持可见（列表最多 10 行高度） */
function scrollActiveIntoView(): void {
  const container = listRef.value
  if (!container) return
  const active = container.querySelector<HTMLElement>('.ip-search__item.is-active')
  active?.scrollIntoView({ block: 'nearest' })
}

/** 类别 → 中文标签 */
const KIND_LABEL: Record<SearchItem['kind'], string> = { nav: '页面', plan: '计划', task: '任务' }
</script>

<template>
  <el-dialog
    v-model="visible"
    width="620px"
    top="8vh"
    :show-close="false"
    append-to-body
    class="ip-search__dialog"
  >
    <template #header>
      <div class="ip-search__head">
        <el-icon><Search /></el-icon>
        <el-input
          ref="inputRef"
          v-model="keyword"
          placeholder="搜索页面 / 计划 / 任务…（↑↓ 选择，Enter 打开，Esc 关闭）"
          clearable
        />
      </div>
    </template>

    <div ref="listRef" class="ip-search__list">
      <div v-if="results.length === 0" class="ip-search__empty">没有匹配结果，试试任务名、计划关键词或页面名。</div>
      <div
        v-for="(item, index) in results"
        :key="item.key"
        class="ip-search__item"
        :class="{ 'is-active': index === activeIndex, 'is-disabled': item.disabled }"
        role="button"
        tabindex="-1"
        @mouseenter="activeIndex = index"
        @click="select(item)"
      >
        <span class="ip-search__kind">{{ KIND_LABEL[item.kind] }}</span>
        <span class="ip-search__main">
          <span class="ip-search__label">{{ item.label }}</span>
          <span class="ip-search__hint">{{ item.hint }}</span>
        </span>
        <span v-if="item.sheet" class="ip-search__sheet">{{ item.sheet }}</span>
      </div>
    </div>
  </el-dialog>
</template>

<style scoped>
.ip-search__head {
  display: flex;
  align-items: center;
  gap: 10px;
}

.ip-search__list {
  max-height: 54vh;
  overflow: auto;
  margin: -6px -8px -8px;
}

.ip-search__empty {
  padding: 26px 12px;
  text-align: center;
  font-size: 12px;
  color: var(--ip-text-muted);
}

.ip-search__item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 12px;
  border-radius: 2px;
  cursor: pointer;
  border-left: 2px solid transparent;
}

.ip-search__item.is-active {
  background: var(--ip-surface-sunk);
  border-left-color: var(--ip-accent);
}

.ip-search__item.is-disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.ip-search__kind {
  flex: none;
  width: 34px;
  font-family: var(--ip-font-mono);
  font-size: 10px;
  letter-spacing: 0.08em;
  color: var(--ip-text-muted);
}

.ip-search__main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.ip-search__label {
  font-size: 13px;
  color: var(--ip-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ip-search__hint {
  font-size: 11px;
  color: var(--ip-text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.ip-search__sheet {
  flex: none;
  font-family: var(--ip-font-mono);
  font-size: 10px;
  padding: 2px 6px;
  color: var(--ip-text-secondary);
  background: var(--ip-surface-sunk);
  border: 1px solid var(--ip-border);
  border-radius: 2px;
}
</style>

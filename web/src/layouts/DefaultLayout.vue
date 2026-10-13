<script setup lang="ts">
/**
 * 控制台主布局：左侧导航 + 顶部图纸抬头 + 计划概况带 + 内容区。
 *
 * 职责：
 *  - 首次进入时加载枚举元数据与计划列表；
 *  - 建立 WebSocket 长连接，实时接收计划变更（其他会话/其他端触发的重规划）；
 *  - 提供全局计划切换、主题切换、通知中心、全局搜索与快捷键体系；
 *  - 提供访问令牌配置入口。
 *
 * 视觉：布局承担「制图台框景」的角色——左侧墨蓝图签栏、顶部图纸抬头（图纸编号 +
 * 标题）、下方计划概况带（等宽数据条），内容区透出纸面网格。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import GlobalSearch from '@/components/GlobalSearch.vue'
import NotifyCenter from '@/components/NotifyCenter.vue'
import ShortcutHelp from '@/components/ShortcutHelp.vue'
import { usePlanStore } from '@/stores/plan'
import { useSettingsStore } from '@/stores/settings'
import { detectMilestones } from '@/utils/analytics'
import { exportCsv, fileStamp } from '@/utils/export'
import { taskExportColumns } from '@/utils/exportPresets'
import { truncate } from '@/utils/format'

const store = usePlanStore()
const settings = useSettingsStore()
const route = useRoute()
const router = useRouter()

/** 令牌配置抽屉 */
const tokenDialogVisible = ref(false)
const tokenDraft = ref(settings.token)

/** 全局搜索 / 快捷键帮助面板 */
const searchVisible = ref(false)
const helpVisible = ref(false)

/** 单计划页面路由名 → 左侧菜单项 key（菜单与路由解耦，避免字符串散落） */
const PLAN_TAB_MENU: Record<string, string> = {
  'plan-overview': 'current-overview',
  'plan-tasks': 'current-tasks',
  'plan-kanban': 'current-kanban',
  'plan-gantt': 'current-gantt',
  'plan-dag': 'current-dag',
  'plan-calendar': 'current-calendar',
  'plan-dashboard': 'current-dashboard',
  'plan-timeline': 'current-timeline',
  'plan-flow': 'current-flow',
  'plan-advisor': 'current-advisor',
  'plan-whatif': 'current-whatif',
  'plan-print': 'current-print',
}

/** 菜单项 key → 路由名（由上一张表反向生成，保证两向一致） */
const MENU_TAB_ROUTE: Record<string, string> = Object.fromEntries(
  Object.entries(PLAN_TAB_MENU).map(([routeName, menuKey]) => [menuKey, routeName]),
)

/** 当前路由名 */
const routeName = computed(() => String(route.name ?? ''))

/** 当前激活菜单项 */
const activeMenu = computed(() => {
  if (PLAN_TAB_MENU[routeName.value]) return PLAN_TAB_MENU[routeName.value]
  if (routeName.value === 'plan-create') return 'plan-create'
  if (routeName.value === 'portfolio') return 'portfolio'
  if (routeName.value === 'audit') return 'audit'
  return 'plan-list'
})

/** 当前图纸编号（取路由 meta.sheet，未登记时回落到目录页编号） */
const sheetNo = computed(() => String(route.meta.sheet ?? 'A-00'))

/** 当前页面标题（直接取路由 meta，避免与路由表重复维护） */
const pageTitle = computed(() => String(route.meta.title ?? '迭代规划控制台'))

/** 当前计划 ID（优先取路由参数，其次取 store 中已加载的计划） */
const currentPlanId = computed(() => (route.params.planId as string | undefined) ?? store.currentPlanId)

/** WebSocket 状态文案（图签 LINK 行） */
const wsText = computed(() => {
  switch (store.wsStatus) {
    case 'open':
      return '实时已连接'
    case 'connecting':
      return '连接中…'
    default:
      return '未连接'
  }
})

/** 计划下拉选项 */
const planOptions = computed(() =>
  store.summaries.map((item) => ({
    value: item.planId,
    label: `${truncate(item.rawRequirement, 28)}（${item.taskCount} 任务）`,
  })),
)

const selectedPlan = computed({
  get: () => currentPlanId.value,
  set: (planId: string) => {
    if (planId && planId !== currentPlanId.value) void router.push({ name: 'plan-overview', params: { planId } })
  },
})

/** 访问令牌状态文案（图签 ACCESS 行） */
const tokenText = computed(() => (settings.token ? '令牌已配置' : '未配置令牌'))

/** 主题切换按钮提示文案 */
const themeText = computed(() => (settings.isDark ? '切换到日间蓝晒' : '切换到夜间台面'))

/** 计划概况带数据（等宽 chips，全部取自引擎返回值或 store 派生） */
const ribbon = computed<{ key: string; value: string; tone?: 'brand' | 'danger' }[]>(() => {
  const current = store.plan
  if (!current) return []
  const stats = store.riskStats
  return [
    { key: '计划', value: truncate(current.rawRequirement, 26) },
    { key: '任务', value: `${current.taskList.length} 项` },
    { key: '关键路径', value: `${store.criticalSet.size} 项`, tone: 'brand' },
    { key: '总工时', value: `${current.totalEstimatedHours}h` },
    { key: '关键工期', value: `${current.totalDurationHours}h / ${store.durationWorkDays}d` },
    {
      key: '风险',
      value: stats.high > 0 ? `${stats.total}（高 ${stats.high}）` : `${stats.total}`,
      tone: stats.high > 0 ? 'danger' : undefined,
    },
    { key: '截止', value: current.iterationDeadline },
    { key: '计划完成', value: store.plannedEndDate ?? '—' },
  ]
})

function go(name: string): void {
  const planRoute = MENU_TAB_ROUTE[name]
  if (planRoute && currentPlanId.value) {
    void router.push({ name: planRoute, params: { planId: currentPlanId.value } })
    return
  }
  const map: Record<string, string> = {
    'plan-list': '/plans',
    'plan-create': '/plans/new',
    portfolio: '/portfolio',
    audit: '/audit',
  }
  void router.push(map[name] ?? '/plans')
}

function openTokenDialog(): void {
  tokenDraft.value = settings.token
  tokenDialogVisible.value = true
}

function saveToken(): void {
  settings.saveToken(tokenDraft.value)
  tokenDialogVisible.value = false
  ElMessage.success('访问令牌已保存')
  void store.loadPlans()
}

async function manualRefresh(): Promise<void> {
  await store.loadPlans()
  if (currentPlanId.value) await store.loadPlan(currentPlanId.value)
  ElMessage.success('已刷新')
}

/* ------------------------------- 快捷键体系 ------------------------------ */

/** 导出当前计划任务清单（CSV，Excel 可直接打开） */
function exportTaskCsv(): void {
  const current = store.plan
  if (!current) {
    ElMessage.warning('请先打开一个迭代计划，再执行导出')
    return
  }
  const milestones = detectMilestones(current)
  const riskyIds = new Set(current.riskList.flatMap((risk) => risk.taskIds))
  exportCsv(
    `iteration-tasks-${current.planId}.${fileStamp()}.csv`,
    taskExportColumns({ criticalIds: new Set(current.criticalPath), milestoneIds: new Set(milestones.map((item) => item.taskId)), riskyIds }),
    current.taskList,
  )
  ElMessage.success(`已导出 ${current.taskList.length} 条任务`)
}

/** 跳转打印友好报告页并自动唤起打印 */
function openPrintReport(): void {
  if (!currentPlanId.value) {
    ElMessage.warning('请先打开一个迭代计划，再打印报告')
    return
  }
  void router.push({ name: 'plan-print', params: { planId: currentPlanId.value }, query: { autoprint: '1' } })
}

/**
 * 全局键盘监听。
 *
 * 约定：带 Ctrl/⌘ 的组合键在任何焦点下都生效；`?` 仅在非输入状态下生效，
 * 避免在文本框中误触（输入框中的 `?` 是普通字符）。
 */
function onGlobalKeydown(event: KeyboardEvent): void {
  const target = event.target as HTMLElement | null
  const typing =
    !!target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable === true)
  const mod = event.ctrlKey || event.metaKey
  const key = event.key.toLowerCase()

  if (mod && !event.shiftKey && key === 'k') {
    event.preventDefault()
    searchVisible.value = true
    return
  }
  if (mod && event.shiftKey && key === 'l') {
    event.preventDefault()
    settings.toggleTheme()
    return
  }
  if (mod && event.shiftKey && key === 'e') {
    event.preventDefault()
    exportTaskCsv()
    return
  }
  if (mod && event.shiftKey && key === 'h') {
    event.preventDefault()
    openPrintReport()
    return
  }
  // F1 与 `?` 等价：中文输入法激活时 Shift+/ 可能被输入法拦截，F1 作为稳定入口
  if (!mod && (event.key === 'F1' || (!typing && event.key === '?'))) {
    event.preventDefault()
    helpVisible.value = true
  }
}

/** 路由变化时同步拉取计划详情 */
watch(
  () => route.params.planId,
  (planId) => {
    if (typeof planId === 'string' && planId) void store.loadPlan(planId)
  },
  { immediate: true },
)

onMounted(() => {
  void store.loadEnums()
  void store.loadPlans()
  store.connect()
  window.addEventListener('keydown', onGlobalKeydown)
})

onBeforeUnmount(() => {
  store.disconnect()
  window.removeEventListener('keydown', onGlobalKeydown)
})
</script>

<template>
  <el-container style="height: 100%">
    <el-aside width="232px" class="ip-aside">
      <div class="ip-brand">
        <el-icon class="ip-brand__mark" :size="20"><Compass /></el-icon>
        <div>
          <div class="ip-brand__title">迭代规划控制台</div>
          <div class="ip-brand__sub">Iteration Planner</div>
        </div>
      </div>

      <el-menu :default-active="activeMenu" class="ip-menu" @select="go">
        <el-menu-item index="plan-list">
          <el-icon><List /></el-icon><span>计划列表</span>
        </el-menu-item>
        <el-menu-item index="plan-create">
          <el-icon><MagicStick /></el-icon><span>新建计划</span>
        </el-menu-item>
        <el-menu-item index="portfolio">
          <el-icon><Grid /></el-icon><span>项目组合对比</span>
        </el-menu-item>
        <el-sub-menu index="current" :disabled="!currentPlanId">
          <template #title>
            <el-icon><DataAnalysis /></el-icon><span>当前计划</span>
          </template>
          <el-menu-item index="current-overview">计划总览</el-menu-item>
          <el-menu-item index="current-tasks">任务管理</el-menu-item>
          <el-menu-item index="current-kanban">任务看板</el-menu-item>
          <el-menu-item index="current-gantt">甘特图</el-menu-item>
          <el-menu-item index="current-dag">依赖 DAG</el-menu-item>
          <el-menu-item index="current-calendar">任务日历</el-menu-item>
        </el-sub-menu>
        <el-sub-menu index="insight" :disabled="!currentPlanId">
          <template #title>
            <el-icon><TrendCharts /></el-icon><span>分析与洞察</span>
          </template>
          <el-menu-item index="current-dashboard">迭代仪表盘</el-menu-item>
          <el-menu-item index="current-timeline">活动时间线</el-menu-item>
          <el-menu-item index="current-flow">度量分析（CFD / 速度）</el-menu-item>
          <el-menu-item index="current-advisor">风险缓解建议</el-menu-item>
          <el-menu-item index="current-whatif">What-if 情景对比</el-menu-item>
        </el-sub-menu>
        <el-sub-menu index="deliver" :disabled="!currentPlanId">
          <template #title>
            <el-icon><Printer /></el-icon><span>交付与归档</span>
          </template>
          <el-menu-item index="current-print">报告打印</el-menu-item>
        </el-sub-menu>
        <el-menu-item index="audit">
          <el-icon><Tickets /></el-icon><span>审计日志</span>
        </el-menu-item>
      </el-menu>

      <!-- 图签：连接状态与访问配置（制图纸右下角的签署栏） -->
      <div class="ip-aside__foot">
        <div class="ip-foot__row">
          <span class="ip-foot__label">Link</span>
          <span class="ip-foot__value">
            <i class="ip-foot__dot" :class="`is-${store.wsStatus}`" />{{ wsText }}
          </span>
        </div>
        <button type="button" class="ip-foot__row is-action" @click="openTokenDialog">
          <span class="ip-foot__label">Access</span>
          <span class="ip-foot__value">{{ tokenText }}</span>
        </button>
        <button type="button" class="ip-foot__row is-action" @click="helpVisible = true">
          <span class="ip-foot__label">Keys</span>
          <span class="ip-foot__value">快捷键 ?</span>
        </button>
      </div>
    </el-aside>

    <el-container>
      <el-header class="ip-header">
        <div class="ip-header__lead">
          <span class="ip-header__sheet">{{ sheetNo }}</span>
          <div>
            <div class="ip-header__eyebrow ip-label">迭代图纸 · Iteration Sheet</div>
            <h1 class="ip-header__title">{{ pageTitle }}</h1>
          </div>
        </div>
        <div class="ip-header__right">
          <el-button class="ip-search-trigger" @click="searchVisible = true">
            <el-icon><Search /></el-icon>
            <span>搜索</span>
            <span class="ip-search-trigger__keys">Ctrl K</span>
          </el-button>
          <el-select
            v-model="selectedPlan"
            placeholder="选择计划"
            filterable
            clearable
            style="width: 260px"
            :no-data-text="'暂无计划，请先新建'"
          >
            <el-option v-for="item in planOptions" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
          <el-tooltip :content="themeText" placement="bottom">
            <el-button :icon="settings.isDark ? 'Sunny' : 'Moon'" circle @click="settings.toggleTheme()" />
          </el-tooltip>
          <NotifyCenter />
          <el-tooltip content="快捷键与操作说明（?）" placement="bottom">
            <el-button :icon="'QuestionFilled'" circle @click="helpVisible = true" />
          </el-tooltip>
          <el-tooltip content="刷新当前数据" placement="bottom">
            <el-button :icon="'Refresh'" circle @click="manualRefresh" />
          </el-tooltip>
          <el-tooltip content="配置访问令牌 / 打开接口文档" placement="bottom">
            <el-button :icon="'Key'" circle @click="openTokenDialog" />
          </el-tooltip>
        </div>
      </el-header>

      <div v-if="store.plan" class="ip-ribbon">
        <span v-for="chip in ribbon" :key="chip.key" class="ip-ribbon__chip">
          <span class="ip-ribbon__key">{{ chip.key }}</span>
          <span class="ip-ribbon__val" :class="chip.tone ? `is-${chip.tone}` : undefined">{{ chip.value }}</span>
        </span>
      </div>

      <el-main class="ip-main">
        <router-view v-loading="store.detailLoading" />
      </el-main>
    </el-container>

    <GlobalSearch v-model="searchVisible" />
    <ShortcutHelp v-model="helpVisible" />

    <el-dialog v-model="tokenDialogVisible" title="访问配置" width="480px">
      <el-form label-width="96px">
        <el-form-item label="访问令牌">
          <el-input v-model="tokenDraft" type="password" show-password placeholder="与后端 API_TOKEN 一致；未启用鉴权时留空" />
        </el-form-item>
        <el-form-item label="接口文档">
          <el-link type="primary" :href="'/api/docs'" target="_blank">在新标签打开 Swagger UI</el-link>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="tokenDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="saveToken">保存</el-button>
      </template>
    </el-dialog>
  </el-container>
</template>

<style scoped>
/* 侧栏：墨蓝渐变 + 24px 制图网格底纹 */
.ip-aside {
  color: #d3e0e9;
  display: flex;
  flex-direction: column;
  border-right: 1px solid var(--ip-ink-800);
  background-color: var(--ip-ink-950);
  background-image:
    linear-gradient(rgba(255, 255, 255, 0.03) 1px, transparent 1px),
    linear-gradient(90deg, rgba(255, 255, 255, 0.03) 1px, transparent 1px),
    linear-gradient(180deg, var(--ip-ink-900) 0%, var(--ip-ink-950) 100%);
  background-size: 24px 24px, 24px 24px, 100% 100%;
}

/* 品牌区：顶部品牌色轨 + 制图体标题 */
.ip-brand {
  position: relative;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 20px 16px 18px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}

.ip-brand::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  width: 92px;
  height: 2px;
  background: linear-gradient(90deg, var(--ip-brand) 0, var(--ip-brand) 52px, var(--ip-accent) 52px, var(--ip-accent) 92px);
}

.ip-brand__mark {
  color: var(--ip-accent);
}

.ip-brand__title {
  font-family: var(--ip-font-display);
  font-size: 15px;
  font-weight: 600;
  letter-spacing: 0.04em;
  color: #ffffff;
}

.ip-brand__sub {
  font-family: var(--ip-font-mono);
  font-size: 10px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: #7fa6bd;
}

/* 菜单：暗底描线，选中项为青色左条 + 渐变底 */
.ip-menu {
  flex: 1;
  overflow: auto;
  background: transparent;
  border-right: none;
  --el-menu-bg-color: transparent;
  --el-menu-text-color: #b3c9d8;
  --el-menu-hover-bg-color: rgba(255, 255, 255, 0.06);
  --el-menu-hover-text-color: #ffffff;
  --el-menu-active-color: #ffffff;
  --el-menu-item-height: 44px;
  --el-menu-sub-item-height: 38px;
}

:deep(.ip-menu .el-menu-item) {
  position: relative;
  font-size: 13px;
  letter-spacing: 0.03em;
}

:deep(.ip-menu .el-menu-item.is-active) {
  background: linear-gradient(90deg, rgba(23, 162, 184, 0.22) 0%, rgba(23, 162, 184, 0.02) 100%);
}

:deep(.ip-menu .el-menu-item.is-active)::before {
  content: '';
  position: absolute;
  top: 0;
  bottom: 0;
  left: 0;
  width: 3px;
  background: var(--ip-accent);
}

:deep(.ip-menu .el-sub-menu__title) {
  font-size: 13px;
  letter-spacing: 0.03em;
  color: #b3c9d8;
}

:deep(.ip-menu .el-sub-menu__title:hover) {
  color: #ffffff;
}

:deep(.ip-menu .el-sub-menu .el-menu-item) {
  padding-left: 44px !important;
}

/* 图签（侧栏底部签署栏） */
.ip-aside__foot {
  border-top: 1px solid rgba(255, 255, 255, 0.08);
  padding: 10px 16px 14px;
}

.ip-foot__row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  width: 100%;
  padding: 5px 6px;
  border: none;
  border-radius: 2px;
  background: none;
  font: inherit;
  text-align: left;
  color: inherit;
}

.ip-foot__row.is-action {
  cursor: pointer;
}

.ip-foot__row.is-action:hover {
  background: rgba(255, 255, 255, 0.06);
}

.ip-foot__label {
  font-family: var(--ip-font-mono);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: #7fa6bd;
}

.ip-foot__value {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: #d3e0e9;
}

.ip-foot__dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--ip-info);
}

.ip-foot__dot.is-open {
  background: var(--ip-success);
  box-shadow: 0 0 0 3px rgba(47, 158, 107, 0.2);
}

.ip-foot__dot.is-connecting {
  background: var(--ip-warning);
  box-shadow: 0 0 0 3px rgba(224, 139, 26, 0.2);
}

/* 顶部抬头：半透纸面 + 图纸编号铭牌 */
.ip-header {
  --el-header-height: 72px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 0 24px;
  background: rgba(255, 255, 255, 0.88);
  backdrop-filter: blur(6px);
  border-bottom: 1px solid var(--ip-border-strong);
}

.ip-header__lead {
  display: flex;
  align-items: center;
  gap: 14px;
  min-width: 0;
}

.ip-header__sheet {
  font-family: var(--ip-font-mono);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.12em;
  padding: 5px 9px;
  color: var(--ip-ink-700);
  background: var(--ip-surface-sunk);
  border: 1px solid var(--ip-border-strong);
  border-radius: 2px;
}

.ip-header__eyebrow {
  display: block;
  margin-bottom: 1px;
}

.ip-header__title {
  font-family: var(--ip-font-display);
  font-size: 17px;
  font-weight: 600;
  letter-spacing: 0.03em;
  color: var(--ip-ink-900);
}

.ip-header__right {
  display: flex;
  align-items: center;
  gap: 10px;
}

/* 搜索触发按钮：仿命令面板的样式 */
.ip-search-trigger {
  gap: 6px;
}

.ip-search-trigger__keys {
  font-family: var(--ip-font-mono);
  font-size: 10px;
  padding: 2px 5px;
  margin-left: 2px;
  color: var(--ip-text-muted);
  border: 1px solid var(--ip-border);
  border-radius: 2px;
}

/* 计划概况带：等宽数据条，分栏以细线分隔 */
.ip-ribbon {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  padding: 7px 24px;
  background: var(--ip-surface);
  border-bottom: 1px solid var(--ip-border);
}

.ip-ribbon__chip {
  display: flex;
  align-items: baseline;
  gap: 6px;
  padding: 0 14px;
  border-left: 1px solid var(--ip-border);
}

.ip-ribbon__chip:first-child {
  padding-left: 0;
  border-left: none;
}

.ip-ribbon__key {
  font-family: var(--ip-font-mono);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--ip-text-muted);
}

.ip-ribbon__val {
  font-family: var(--ip-font-mono);
  font-size: 12px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  color: var(--ip-ink-800);
}

.ip-ribbon__val.is-brand {
  color: var(--ip-brand);
}

.ip-ribbon__val.is-danger {
  color: var(--ip-danger);
}

/* 内容区：透出 body 制图纸网格 */
.ip-main {
  background: transparent;
  padding: 0;
  overflow: auto;
}
</style>

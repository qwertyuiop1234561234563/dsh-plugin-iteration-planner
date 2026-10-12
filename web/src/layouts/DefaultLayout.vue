<script setup lang="ts">
/**
 * 控制台主布局：左侧导航 + 顶部图纸抬头 + 计划概况带 + 内容区。
 *
 * 职责：
 *  - 首次进入时加载枚举元数据与计划列表；
 *  - 建立 WebSocket 长连接，实时接收计划变更（其他会话/其他端触发的重规划）；
 *  - 提供全局计划切换与访问令牌配置入口。
 *
 * 视觉：布局承担「制图台框景」的角色——左侧墨蓝图签栏、顶部图纸抬头（图纸编号 +
 * 标题）、下方计划概况带（等宽数据条），内容区透出纸面网格。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { usePlanStore } from '@/stores/plan'
import { useSettingsStore } from '@/stores/settings'
import { truncate } from '@/utils/format'

const store = usePlanStore()
const settings = useSettingsStore()
const route = useRoute()
const router = useRouter()

/** 令牌配置抽屉 */
const tokenDialogVisible = ref(false)
const tokenDraft = ref(settings.token)

/**
 * 图纸编号：为每个视图分配制图册式图号（A=台账、B=施工、C=分析、D=归档）。
 * 这是纯粹的视觉编号，不参与任何业务判定。
 */
const SHEET_INDEX: Record<string, string> = {
  'plan-list': 'A-01',
  'plan-create': 'A-02',
  'plan-overview': 'B-01',
  'plan-tasks': 'B-02',
  'plan-kanban': 'B-03',
  'plan-gantt': 'B-04',
  'plan-dag': 'B-05',
  'plan-dashboard': 'C-01',
  'plan-timeline': 'C-02',
  'plan-advisor': 'C-03',
  'plan-whatif': 'C-04',
  audit: 'D-01',
}

/** 单计划页面路由名 → 左侧菜单项 key（菜单与路由解耦，避免字符串散落） */
const PLAN_TAB_MENU: Record<string, string> = {
  'plan-overview': 'current-overview',
  'plan-tasks': 'current-tasks',
  'plan-dag': 'current-dag',
  'plan-gantt': 'current-gantt',
  'plan-kanban': 'current-kanban',
  'plan-dashboard': 'current-dashboard',
  'plan-timeline': 'current-timeline',
  'plan-advisor': 'current-advisor',
  'plan-whatif': 'current-whatif',
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
  if (routeName.value === 'audit') return 'audit'
  return 'plan-list'
})

/** 当前图纸编号（目录页也给出编号，保持抬头一致） */
const sheetNo = computed(() => SHEET_INDEX[routeName.value] ?? 'A-00')

/** 当前页面标题（直接取路由 meta，避免与路由表重复维护） */
const pageTitle = computed(() => String(route.meta.title ?? '迭代规划控制台'))

/** 当前计划路由参数 */
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
  const map: Record<string, string> = { 'plan-list': '/plans', 'plan-create': '/plans/new', audit: '/audit' }
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
})

onBeforeUnmount(() => {
  store.disconnect()
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
        <el-sub-menu index="current" :disabled="!currentPlanId">
          <template #title>
            <el-icon><DataAnalysis /></el-icon><span>当前计划</span>
          </template>
          <el-menu-item index="current-overview">计划总览</el-menu-item>
          <el-menu-item index="current-tasks">任务管理</el-menu-item>
          <el-menu-item index="current-kanban">任务看板</el-menu-item>
          <el-menu-item index="current-gantt">甘特图</el-menu-item>
          <el-menu-item index="current-dag">依赖 DAG</el-menu-item>
        </el-sub-menu>
        <el-sub-menu index="insight" :disabled="!currentPlanId">
          <template #title>
            <el-icon><TrendCharts /></el-icon><span>分析与洞察</span>
          </template>
          <el-menu-item index="current-dashboard">迭代仪表盘</el-menu-item>
          <el-menu-item index="current-timeline">活动时间线</el-menu-item>
          <el-menu-item index="current-advisor">风险缓解建议</el-menu-item>
          <el-menu-item index="current-whatif">What-if 情景对比</el-menu-item>
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
          <el-select
            v-model="selectedPlan"
            placeholder="选择计划"
            filterable
            clearable
            style="width: 320px"
            :no-data-text="'暂无计划，请先新建'"
          >
            <el-option v-for="item in planOptions" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
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

    <el-dialog v-model="tokenDialogVisible" title="访问配置" width="480px">
      <el-form label-width="96px">
        <el-form-item label="访问令牌">
          <el-input v-model="tokenDraft" type="password" show-password placeholder="与后端 API_TOKEN 一致；未启用鉴权时留空" />
        </el-form-item>
        <el-form-item label="接口文档">
          <el-link type="primary" :href="'/api/docs'" target="_blank">在新区块打开 Swagger UI</el-link>
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
  gap: 12px;
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
<script setup lang="ts">
/**
 * 控制台主布局：左侧导航 + 顶部状态栏 + 内容区。
 *
 * 职责：
 *  - 首次进入时加载枚举元数据与计划列表；
 *  - 建立 WebSocket 长连接，实时接收计划变更（其他会话/其他端触发的重规划）；
 *  - 提供全局计划切换与访问令牌配置入口。
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

/** 当前激活菜单项 */
const activeMenu = computed(() => {
  const path = route.path
  if (path.startsWith('/plans/') && route.name === 'plan-overview') return 'current-overview'
  if (route.name === 'plan-tasks') return 'current-tasks'
  if (route.name === 'plan-dag') return 'current-dag'
  if (path === '/plans/new') return 'plan-create'
  if (path === '/plans') return 'plan-list'
  if (path === '/audit') return 'audit'
  return 'plan-list'
})

/** 当前计划路由参数 */
const currentPlanId = computed(() => (route.params.planId as string | undefined) ?? store.currentPlanId)

/** WebSocket 状态展示 */
const wsTag = computed(() => {
  switch (store.wsStatus) {
    case 'open':
      return { type: 'success' as const, text: '实时已连接' }
    case 'connecting':
      return { type: 'warning' as const, text: '连接中…' }
    default:
      return { type: 'info' as const, text: '未连接' }
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

function go(name: string): void {
  if (name.startsWith('current-') && currentPlanId.value) {
    const suffix = name.replace('current-', '')
    const routeName = suffix === 'overview' ? 'plan-overview' : suffix === 'tasks' ? 'plan-tasks' : 'plan-dag'
    void router.push({ name: routeName, params: { planId: currentPlanId.value } })
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
    <el-aside width="216px" class="ip-aside">
      <div class="ip-brand">
        <el-icon :size="22"><Compass /></el-icon>
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
          <el-menu-item index="current-dag">依赖 DAG</el-menu-item>
        </el-sub-menu>
        <el-menu-item index="audit">
          <el-icon><Tickets /></el-icon><span>审计日志</span>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header class="ip-header">
        <div class="ip-header__left">
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
          <el-tag :type="wsTag.type" effect="plain" size="small">{{ wsTag.text }}</el-tag>
        </div>
        <div class="ip-header__right">
          <el-tooltip content="刷新当前数据" placement="bottom">
            <el-button :icon="'Refresh'" circle @click="manualRefresh" />
          </el-tooltip>
          <el-tooltip content="配置访问令牌 / 打开接口文档" placement="bottom">
            <el-button :icon="'Key'" circle @click="openTokenDialog" />
          </el-tooltip>
        </div>
      </el-header>

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
.ip-aside {
  background: #1f2d3d;
  color: #e6eaf2;
  display: flex;
  flex-direction: column;
}

.ip-brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 18px 16px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}

.ip-brand__title {
  font-size: 15px;
  font-weight: 600;
  color: #fff;
}

.ip-brand__sub {
  font-size: 11px;
  color: #8f9bb3;
  letter-spacing: 0.5px;
}

.ip-menu {
  flex: 1;
  background: transparent;
  border-right: none;
  --el-menu-text-color: #c8d1e0;
  --el-menu-hover-bg-color: rgba(255, 255, 255, 0.08);
  --el-menu-active-color: #ffffff;
}

:deep(.ip-menu .el-menu-item.is-active) {
  background: var(--ip-brand);
}

:deep(.ip-menu .el-sub-menu__title) {
  color: #c8d1e0;
}

.ip-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: var(--ip-surface);
  border-bottom: 1px solid var(--ip-border);
}

.ip-header__left,
.ip-header__right {
  display: flex;
  align-items: center;
  gap: 12px;
}

.ip-main {
  background: var(--ip-bg);
  padding: 0;
  overflow: auto;
}
</style>
/**
 * 路由配置（history 模式，生产环境由 API 服务托管并回落 index.html）。
 *
 * 约定：所有「单计划」页面都挂在 `plans/:planId/*` 下，便于共享计划上下文、
 * 左右导航高亮与刷新逻辑（布局层监听 `route.params.planId` 自动拉取详情）。
 *
 * `meta.sheet` 为制图册式图号（A=台账、B=施工、C=分析、D=归档）：抬头铭牌与
 * 全局搜索的命令列表都从此处派生，避免同一份菜单在多个文件里重复维护。
 *
 * `meta.permission` 声明访问该页所需的权限标识（与后端 `app/constants.py` 对齐）。
 * 路由守卫据此在**客户端**做一次收敛，但真正的拦截在服务端 —— 本地改动只会得到
 * 一个能渲染但取不到数据的空页面，不会获得任何越权能力。
 */

import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import DefaultLayout from '@/layouts/DefaultLayout.vue'
import AdvisorView from '@/views/AdvisorView.vue'
import AuditView from '@/views/AuditView.vue'
import BaselineView from '@/views/BaselineView.vue'
import BurndownAnalysisView from '@/views/BurndownAnalysisView.vue'
import CalendarView from '@/views/CalendarView.vue'
import DagView from '@/views/DagView.vue'
import DashboardView from '@/views/DashboardView.vue'
import FlowView from '@/views/FlowView.vue'
import GanttView from '@/views/GanttView.vue'
import KanbanView from '@/views/KanbanView.vue'
import KnowledgeView from '@/views/KnowledgeView.vue'
import LoginView from '@/views/LoginView.vue'
import MemoryView from '@/views/MemoryView.vue'
import QualityView from '@/views/QualityView.vue'
import RiskRegisterView from '@/views/RiskRegisterView.vue'
import UserManageView from '@/views/UserManageView.vue'
import WorkloadView from '@/views/WorkloadView.vue'
import PlanCreateView from '@/views/PlanCreateView.vue'
import PlanListView from '@/views/PlanListView.vue'
import PlanOverviewView from '@/views/PlanOverviewView.vue'
import PortfolioView from '@/views/PortfolioView.vue'
import ReportPrintView from '@/views/ReportPrintView.vue'
import TaskTableView from '@/views/TaskTableView.vue'
import TimelineView from '@/views/TimelineView.vue'
import WhatIfView from '@/views/WhatIfView.vue'
import { useAuthStore } from '@/stores/auth'
import type { AuthPermission } from '@/api/types'

/** 路由元信息扩展：权限声明与「仅访客可见范围」标记 */
declare module 'vue-router' {
  interface RouteMeta {
    title?: string
    sheet?: string
    /** 访问该页所需权限（缺省表示仅需登录） */
    permission?: AuthPermission
    /** 是否为免登录页（登录页自身） */
    public?: boolean
    /** 页面内是否包含写操作（写权限由页面内按钮级 `can('plan:write')` 收敛） */
    writable?: boolean
  }
}

/** 计划详情类页面的统一声明（全部只需 plan:read） */
function planRoutes(): RouteRecordRaw[] {
  return [
    { path: 'plans/:planId', name: 'plan-overview', component: PlanOverviewView, meta: { title: '计划总览', sheet: 'B-01', permission: 'plan:read' } },
    { path: 'plans/:planId/tasks', name: 'plan-tasks', component: TaskTableView, meta: { title: '任务管理', sheet: 'B-02', permission: 'plan:read', writable: true } },
    { path: 'plans/:planId/kanban', name: 'plan-kanban', component: KanbanView, meta: { title: '任务看板', sheet: 'B-03', permission: 'plan:read', writable: true } },
    { path: 'plans/:planId/gantt', name: 'plan-gantt', component: GanttView, meta: { title: '甘特图', sheet: 'B-04', permission: 'plan:read' } },
    { path: 'plans/:planId/dag', name: 'plan-dag', component: DagView, meta: { title: '依赖 DAG', sheet: 'B-05', permission: 'plan:read' } },
    { path: 'plans/:planId/calendar', name: 'plan-calendar', component: CalendarView, meta: { title: '任务日历', sheet: 'B-06', permission: 'plan:read' } },
    { path: 'plans/:planId/baseline', name: 'plan-baseline', component: BaselineView, meta: { title: '基线快照与偏差', sheet: 'B-07', permission: 'plan:read', writable: true } },
    { path: 'plans/:planId/dashboard', name: 'plan-dashboard', component: DashboardView, meta: { title: '迭代仪表盘', sheet: 'C-01', permission: 'plan:read' } },
    { path: 'plans/:planId/timeline', name: 'plan-timeline', component: TimelineView, meta: { title: '活动时间线', sheet: 'C-02', permission: 'plan:read' } },
    { path: 'plans/:planId/flow', name: 'plan-flow', component: FlowView, meta: { title: '度量分析', sheet: 'C-03', permission: 'plan:read' } },
    { path: 'plans/:planId/advisor', name: 'plan-advisor', component: AdvisorView, meta: { title: '风险缓解建议', sheet: 'C-04', permission: 'plan:read', writable: true } },
    { path: 'plans/:planId/whatif', name: 'plan-whatif', component: WhatIfView, meta: { title: 'What-if 情景对比', sheet: 'C-05', permission: 'plan:read', writable: true } },
    { path: 'plans/:planId/workload', name: 'plan-workload', component: WorkloadView, meta: { title: '人员负荷与资源平衡', sheet: 'C-06', permission: 'plan:read' } },
    { path: 'plans/:planId/burndown', name: 'plan-burndown', component: BurndownAnalysisView, meta: { title: '燃尽增强分析', sheet: 'C-07', permission: 'plan:read' } },
    { path: 'plans/:planId/quality', name: 'plan-quality', component: QualityView, meta: { title: '交付质量看板', sheet: 'C-08', permission: 'plan:read' } },
    { path: 'plans/:planId/registers', name: 'plan-registers', component: RiskRegisterView, meta: { title: '风险登记册', sheet: 'C-09', permission: 'plan:read', writable: true } },
    { path: 'plans/:planId/print', name: 'plan-print', component: ReportPrintView, meta: { title: '报告打印', sheet: 'D-02', permission: 'plan:read' } },
  ]
}

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: LoginView,
      meta: { title: '登录', public: true },
    },
    {
      path: '/',
      component: DefaultLayout,
      children: [
        { path: '', redirect: '/plans' },
        {
          path: 'plans',
          name: 'plan-list',
          component: PlanListView,
          meta: { title: '计划列表', sheet: 'A-01', permission: 'plan:read' },
        },
        {
          path: 'plans/new',
          name: 'plan-create',
          component: PlanCreateView,
          meta: { title: '新建计划', sheet: 'A-02', permission: 'plan:write' },
        },
        {
          path: 'portfolio',
          name: 'portfolio',
          component: PortfolioView,
          meta: { title: '项目组合对比', sheet: 'A-03', permission: 'plan:read' },
        },
        {
          path: 'knowledge',
          name: 'knowledge',
          component: KnowledgeView,
          meta: { title: '知识库', sheet: 'E-01', permission: 'kb:read', writable: true },
        },
        {
          path: 'memory',
          name: 'memory',
          component: MemoryView,
          meta: { title: '长期记忆', sheet: 'E-02', permission: 'memory:read', writable: true },
        },
        {
          path: 'admin/users',
          name: 'admin-users',
          component: UserManageView,
          meta: { title: '账号与权限', sheet: 'F-01', permission: 'user:manage' },
        },
        ...planRoutes(),
        {
          path: 'audit',
          name: 'audit',
          component: AuditView,
          meta: { title: '审计日志', sheet: 'D-01', permission: 'audit:read' },
        },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/plans' },
  ],
})

/**
 * 全局前置守卫：会话复原 → 登录校验 → 权限校验 → 计划可见范围校验。
 *
 * 注意 `ready` 标志：页面刷新后本地无用户态，必须**先**用 `refresh()` 向服务端复原会话，
 * 否则会把「尚未复原」误判成「未登录」而把已登录用户弹回登录页（刷新死循环）。
 */
router.beforeEach(async (to) => {
  const auth = useAuthStore()

  if (!auth.ready) await auth.refresh()

  if (to.meta.public) {
    // 已登录访问登录页时直接进入控制台，避免重复登录
    return auth.isAuthenticated ? { path: '/plans' } : true
  }

  if (!auth.isAuthenticated) {
    return { name: 'login', query: to.fullPath === '/' ? {} : { redirect: to.fullPath } }
  }

  if (to.meta.permission && !auth.can(to.meta.permission)) {
    return { path: '/plans', query: { denied: to.fullPath } }
  }

  // 访客只能打开被指派的计划；越权按「不可见」处理（不提示存在性）
  const planId = to.params.planId
  if (typeof planId === 'string' && planId && !auth.canSeePlan(planId)) {
    return { path: '/plans', query: { denied: to.fullPath } }
  }

  return true
})

export default router

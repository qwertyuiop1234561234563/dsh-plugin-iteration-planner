/**
 * 路由配置（history 模式，生产环境由 API 服务托管并回落 index.html）。
 *
 * 约定：所有「单计划」页面都挂在 `plans/:planId/*` 下，便于共享计划上下文、
 * 左右导航高亮与刷新逻辑（布局层监听 `route.params.planId` 自动拉取详情）。
 *
 * `meta.sheet` 为制图册式图号（A=台账、B=施工、C=分析、D=归档）：抬头铭牌与
 * 全局搜索的命令列表都从此处派生，避免同一份菜单在多个文件里重复维护。
 */

import { createRouter, createWebHistory } from 'vue-router'
import DefaultLayout from '@/layouts/DefaultLayout.vue'
import AdvisorView from '@/views/AdvisorView.vue'
import AuditView from '@/views/AuditView.vue'
import CalendarView from '@/views/CalendarView.vue'
import DagView from '@/views/DagView.vue'
import DashboardView from '@/views/DashboardView.vue'
import FlowView from '@/views/FlowView.vue'
import GanttView from '@/views/GanttView.vue'
import KanbanView from '@/views/KanbanView.vue'
import PlanCreateView from '@/views/PlanCreateView.vue'
import PlanListView from '@/views/PlanListView.vue'
import PlanOverviewView from '@/views/PlanOverviewView.vue'
import PortfolioView from '@/views/PortfolioView.vue'
import ReportPrintView from '@/views/ReportPrintView.vue'
import TaskTableView from '@/views/TaskTableView.vue'
import TimelineView from '@/views/TimelineView.vue'
import WhatIfView from '@/views/WhatIfView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      component: DefaultLayout,
      children: [
        { path: '', redirect: '/plans' },
        {
          path: 'plans',
          name: 'plan-list',
          component: PlanListView,
          meta: { title: '计划列表', sheet: 'A-01' },
        },
        {
          path: 'plans/new',
          name: 'plan-create',
          component: PlanCreateView,
          meta: { title: '新建计划', sheet: 'A-02' },
        },
        {
          path: 'portfolio',
          name: 'portfolio',
          component: PortfolioView,
          meta: { title: '项目组合对比', sheet: 'A-03' },
        },
        {
          path: 'plans/:planId',
          name: 'plan-overview',
          component: PlanOverviewView,
          meta: { title: '计划总览', sheet: 'B-01' },
        },
        {
          path: 'plans/:planId/tasks',
          name: 'plan-tasks',
          component: TaskTableView,
          meta: { title: '任务管理', sheet: 'B-02' },
        },
        {
          path: 'plans/:planId/kanban',
          name: 'plan-kanban',
          component: KanbanView,
          meta: { title: '任务看板', sheet: 'B-03' },
        },
        {
          path: 'plans/:planId/gantt',
          name: 'plan-gantt',
          component: GanttView,
          meta: { title: '甘特图', sheet: 'B-04' },
        },
        {
          path: 'plans/:planId/dag',
          name: 'plan-dag',
          component: DagView,
          meta: { title: '依赖 DAG', sheet: 'B-05' },
        },
        {
          path: 'plans/:planId/calendar',
          name: 'plan-calendar',
          component: CalendarView,
          meta: { title: '任务日历', sheet: 'B-06' },
        },
        {
          path: 'plans/:planId/dashboard',
          name: 'plan-dashboard',
          component: DashboardView,
          meta: { title: '迭代仪表盘', sheet: 'C-01' },
        },
        {
          path: 'plans/:planId/timeline',
          name: 'plan-timeline',
          component: TimelineView,
          meta: { title: '活动时间线', sheet: 'C-02' },
        },
        {
          path: 'plans/:planId/flow',
          name: 'plan-flow',
          component: FlowView,
          meta: { title: '度量分析', sheet: 'C-03' },
        },
        {
          path: 'plans/:planId/advisor',
          name: 'plan-advisor',
          component: AdvisorView,
          meta: { title: '风险缓解建议', sheet: 'C-04' },
        },
        {
          path: 'plans/:planId/whatif',
          name: 'plan-whatif',
          component: WhatIfView,
          meta: { title: 'What-if 情景对比', sheet: 'C-05' },
        },
        {
          path: 'plans/:planId/print',
          name: 'plan-print',
          component: ReportPrintView,
          meta: { title: '报告打印', sheet: 'D-02' },
        },
        {
          path: 'audit',
          name: 'audit',
          component: AuditView,
          meta: { title: '审计日志', sheet: 'D-01' },
        },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/plans' },
  ],
})

export default router

/**
 * 路由配置（history 模式，生产环境由 API 服务托管并回落 index.html）。
 *
 * 约定：所有「单计划」页面都挂在 `plans/:planId/*` 下，便于共享计划上下文、
 * 左右导航高亮与刷新逻辑（布局层监听 `route.params.planId` 自动拉取详情）。
 */

import { createRouter, createWebHistory } from 'vue-router'
import DefaultLayout from '@/layouts/DefaultLayout.vue'
import AdvisorView from '@/views/AdvisorView.vue'
import AuditView from '@/views/AuditView.vue'
import DagView from '@/views/DagView.vue'
import DashboardView from '@/views/DashboardView.vue'
import GanttView from '@/views/GanttView.vue'
import KanbanView from '@/views/KanbanView.vue'
import PlanCreateView from '@/views/PlanCreateView.vue'
import PlanListView from '@/views/PlanListView.vue'
import PlanOverviewView from '@/views/PlanOverviewView.vue'
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
          meta: { title: '计划列表' },
        },
        {
          path: 'plans/new',
          name: 'plan-create',
          component: PlanCreateView,
          meta: { title: '新建计划' },
        },
        {
          path: 'plans/:planId',
          name: 'plan-overview',
          component: PlanOverviewView,
          meta: { title: '计划总览' },
        },
        {
          path: 'plans/:planId/tasks',
          name: 'plan-tasks',
          component: TaskTableView,
          meta: { title: '任务管理' },
        },
        {
          path: 'plans/:planId/dag',
          name: 'plan-dag',
          component: DagView,
          meta: { title: '依赖 DAG' },
        },
        {
          path: 'plans/:planId/gantt',
          name: 'plan-gantt',
          component: GanttView,
          meta: { title: '甘特图' },
        },
        {
          path: 'plans/:planId/kanban',
          name: 'plan-kanban',
          component: KanbanView,
          meta: { title: '任务看板' },
        },
        {
          path: 'plans/:planId/dashboard',
          name: 'plan-dashboard',
          component: DashboardView,
          meta: { title: '迭代仪表盘' },
        },
        {
          path: 'plans/:planId/timeline',
          name: 'plan-timeline',
          component: TimelineView,
          meta: { title: '活动时间线' },
        },
        {
          path: 'plans/:planId/advisor',
          name: 'plan-advisor',
          component: AdvisorView,
          meta: { title: '风险缓解建议' },
        },
        {
          path: 'plans/:planId/whatif',
          name: 'plan-whatif',
          component: WhatIfView,
          meta: { title: 'What-if 情景对比' },
        },
        {
          path: 'audit',
          name: 'audit',
          component: AuditView,
          meta: { title: '审计日志' },
        },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/plans' },
  ],
})

export default router
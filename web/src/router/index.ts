/**
 * 路由配置（history 模式，生产环境由 API 服务托管并回落 index.html）。
 */

import { createRouter, createWebHistory } from 'vue-router'
import DefaultLayout from '@/layouts/DefaultLayout.vue'
import AuditView from '@/views/AuditView.vue'
import DagView from '@/views/DagView.vue'
import PlanCreateView from '@/views/PlanCreateView.vue'
import PlanListView from '@/views/PlanListView.vue'
import PlanOverviewView from '@/views/PlanOverviewView.vue'
import TaskTableView from '@/views/TaskTableView.vue'

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
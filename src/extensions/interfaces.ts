/**
 * 预留扩展接口（§7.6）：后续可对接 Jira / Git / CI-CD，不写死外部依赖。
 *
 * 本文件只声明接口与数据结构，不包含任何具体实现；
 * 业务服务层通过依赖注入接收这些 provider，缺省时不启用任何外部系统。
 */

import type { ProjectPlan, Task } from '../models/entities.js'

/** 任务来源：可从 Jira / 需求平台拉取既有任务，或把规划结果回写 */
export interface TaskSourceProvider {
  /** provider 名称，用于日志与配置选择 */
  readonly name: string
  /** 拉取任务（例如从 Jira 某个 Sprint 导入） */
  fetchTasks?(query: { sprint?: string; projectKey?: string }): Promise<Task[]>
  /** 回写任务（例如把拆分结果推送到 Jira 创建 issue） */
  pushTasks?(plan: ProjectPlan): Promise<{ created: string[] }>
}

/** 代码仓库联动：可用于校验任务与分支/提交的对应关系 */
export interface RepositoryProvider {
  readonly name: string
  /** 查询与任务相关的最新提交 */
  listCommits?(taskId: string): Promise<Array<{ sha: string; message: string }>>
}

/** CI/CD 联动：可用于把 DEPLOY 任务与流水线状态绑定 */
export interface CiCdProvider {
  readonly name: string
  /** 查询流水线状态 */
  pipelineStatus?(pipelineId: string): Promise<'pending' | 'running' | 'success' | 'failed'>
  /** 触发部署 */
  triggerDeploy?(taskId: string, environment: string): Promise<{ runId: string }>
}

/** 扩展 provider 聚合，供插件配置注入 */
export interface PlannerExtensions {
  taskSource?: TaskSourceProvider
  repository?: RepositoryProvider
  cicd?: CiCdProvider
}
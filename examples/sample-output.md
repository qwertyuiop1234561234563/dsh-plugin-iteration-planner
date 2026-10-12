
==============================================================================
# 企业级研发迭代规划智能 Agent —— 本地 Demo
==============================================================================
拆解策略：规则实现（离线可复现）。接入 Harness 配置 llmProvider/llmModel 后切换为 LLM 主路径。
样例需求：为电商中台开发【订单中心 v2.1】迭代：1) 完成订单创建、支付回调与库存扣减能力；2) 需对接第三方支付网关（外部接口，依赖对方文档与沙箱环境）；3) 支持订单查询与报表统计；4) 优化下单链路性能，并搭建压测环境；5) 产出接口文档与部署说明。
迭代窗口：2026-10-12 ~ 2026-10-30；可用开发人数：3

--- STEP 0 · 插件注册的工具清单（Harness 侧可见 11 个） ---
task_decompose, plan_iteration, replan_all, dag_build, dag_check, schedule_calc, task_add, task_update, task_delete, risk_scan, report_generate

--- STEP 1 · 调用 plan_iteration：拆解 → 缓冲 → DAG → AOE → 风险 → 燃尽 → 报表 ---

【计划结构化摘要】
{
  "planId": "20827101-c31d-480f-b43f-95905404c789",
  "iterationWindow": "2026-10-12 ~ 2026-10-30",
  "devMemberCount": 3,
  "taskCount": 17,
  "totalEstimatedHours": 166.45,
  "totalDurationHours": 57.67,
  "criticalPath": [
    "订单-数据库设计",
    "订单-接口开发",
    "订单-业务代码实现",
    "订单-联调与自测",
    "订单-功能测试",
    "版本部署与发布"
  ],
  "riskCountByLevel": {
    "HIGH": 0,
    "MEDIUM": 4,
    "LOW": 0
  }
}

【工时缓冲修正明细（§3.3）】
- 订单-数据库设计: 6h → 7.59h（命中 TYPE_DB_DESIGN, PRIORITY_P0）
- 订单-接口开发: 12h → 13.2h（命中 PRIORITY_P0）
- 订单-功能测试: 8h → 9.6h（命中 TYPE_TEST）
- 支付-数据库设计: 6h → 7.59h（命中 TYPE_DB_DESIGN, PRIORITY_P0）
- 支付-接口开发: 12h → 13.2h（命中 PRIORITY_P0）
- 支付-功能测试: 8h → 9.6h（命中 TYPE_TEST）
- 库存-数据库设计: 6h → 7.59h（命中 TYPE_DB_DESIGN, PRIORITY_P0）
- 库存-接口开发: 12h → 13.2h（命中 PRIORITY_P0）
- 库存-功能测试: 8h → 9.6h（命中 TYPE_TEST）
- 版本部署与发布: 4h → 5.28h（命中 TYPE_DEPLOY, PRIORITY_P0）

【风险清单】
【MEDIUM】开发人员「dev-2」存在 2 天单日工时超限，峰值 11.21h（2026-10-16）
  └ 整改建议：拆分任务、调整任务并行度或增加人手，确保单人单日总工时不超过 8 小时
【MEDIUM】需求涉及第三方/外部接口对接，外部依赖可能未就绪
  └ 整改建议：提前与第三方确认接口文档、联调窗口与沙箱环境，必要时准备 Mock 降级方案
【MEDIUM】需求可能需要专用测试/联调环境，环境搭建存在不确定性
  └ 整改建议：提前申请或搭建测试环境，纳入迭代前置任务并预留缓冲
【MEDIUM】需求描述存在模糊表述，任务边界可能与预期不一致
  └ 整改建议：在需求评审阶段明确验收标准与边界条件，避免开发中返工

【燃尽图原始数据】
[
  {
    "time": "2026-10-12",
    "remainingHours": 166.45
  },
  {
    "time": "2026-10-13",
    "remainingHours": 145.64
  },
  {
    "time": "2026-10-14",
    "remainingHours": 124.84
  },
  {
    "time": "2026-10-15",
    "remainingHours": 104.03
  },
  {
    "time": "2026-10-16",
    "remainingHours": 83.23
  },
  {
    "time": "2026-10-19",
    "remainingHours": 62.42
  },
  {
    "time": "2026-10-20",
    "remainingHours": 41.61
  },
  {
    "time": "2026-10-21",
    "remainingHours": 20.81
  },
  {
    "time": "2026-10-22",
    "remainingHours": 0
  }
]

【完整 ProjectPlan JSON】
{
  "planId": "20827101-c31d-480f-b43f-95905404c789",
  "rawRequirement": "为电商中台开发【订单中心 v2.1】迭代：1) 完成订单创建、支付回调与库存扣减能力；2) 需对接第三方支付网关（外部接口，依赖对方文档与沙箱环境）；3) 支持订单查询与报表统计；4) 优化下单链路性能，并搭建压测环境；5) 产出接口文档与部署说明。",
  "iterationDeadline": "2026-10-30",
  "iterationStartDate": "2026-10-12",
  "devMemberCount": 3,
  "taskList": [
    {
      "taskId": "d5411625-5bbb-42e3-b1b3-4dd30d594ce6",
      "taskName": "订单-数据库设计",
      "taskDesc": "规则拆解：订单 的数据库设计。来源需求：为电商中台开发【订单中心 v2.1】迭代：1) 完成订单创建、支付回调与库存扣减能力；2) 需对接第三方支付网关（外部接口，依赖对方文档与沙箱环境）；3) 支持",
      "taskType": "DB_DESIGN",
      "estimatedHours": 7.59,
      "predecessorTaskIds": [],
      "status": "TODO",
      "priority": "P0",
      "assignee": "dev-1",
      "createTime": 1791690128015,
      "updateTime": 1791690128015,
      "es": 0,
      "ls": 0,
      "slack": 0
    },
    {
      "taskId": "d83dcbd0-7f58-4be7-87b4-8dab69a88362",
      "taskName": "订单-接口开发",
      "taskDesc": "规则拆解：订单 的接口开发。来源需求：为电商中台开发【订单中心 v2.1】迭代：1) 完成订单创建、支付回调与库存扣减能力；2) 需对接第三方支付网关（外部接口，依赖对方文档与沙箱环境）；3) 支持",
      "taskType": "API_DEV",
      "estimatedHours": 13.2,
      "predecessorTaskIds": [
        "d5411625-5bbb-42e3-b1b3-4dd30d594ce6"
      ],
      "status": "TODO",
      "priority": "P0",
      "assignee": "dev-2",
      "createTime": 1791690128015,
      "updateTime": 1791690128015,
      "es": 7.59,
      "ls": 7.59,
      "slack": 0
    },
    {
      "taskId": "63e1b52b-301c-4ee9-8e67-76f7689c57fb",
      "taskName": "订单-业务代码实现",
      "taskDesc": "规则拆解：订单 的业务代码实现。来源需求：为电商中台开发【订单中心 v2.1】迭代：1) 完成订单创建、支付回调与库存扣减能力；2) 需对接第三方支付网关（外部接口，依赖对方文档与沙箱环境）；3) 支持",
      "taskType": "BUSINESS_CODE",
      "estimatedHours": 16,
      "predecessorTaskIds": [
        "d83dcbd0-7f58-4be7-87b4-8dab69a88362"
      ],
      "status": "TODO",
      "priority": "P1",
      "assignee": "dev-3",
      "createTime": 1791690128015,
      "updateTime": 1791690128015,
      "es": 20.79,
      "ls": 20.79,
      "slack": 0
    },
    {
      "taskId": "6efb8f00-13c8-4ce4-91d2-ac0210a76df1",
      "taskName": "订单-联调与自测",
      "taskDesc": "规则拆解：订单 的联调与自测。来源需求：为电商中台开发【订单中心 v2.1】迭代：1) 完成订单创建、支付回调与库存扣减能力；2) 需对接第三方支付网关（外部接口，依赖对方文档与沙箱环境）；3) 支持",
      "taskType": "DEBUG",
      "estimatedHours": 6,
      "predecessorTaskIds": [
        "63e1b52b-301c-4ee9-8e67-76f7689c57fb"
      ],
      "status": "TODO",
      "priority": "P1",
      "assignee": "dev-1",
      "createTime": 1791690128015,
      "updateTime": 1791690128015,
      "es": 36.79,
      "ls": 36.79,
      "slack": 0
    },
    {
      "taskId": "6a21a9d4-f74b-4dec-b293-c5f5bdba9363",
      "taskName": "订单-功能测试",
      "taskDesc": "规则拆解：订单 的功能测试。来源需求：为电商中台开发【订单中心 v2.1】迭代：1) 完成订单创建、支付回调与库存扣减能力；2) 需对接第三方支付网关（外部接口，依赖对方文档与沙箱环境）；3) 支持",
      "taskType": "TEST",
      "estimatedHours": 9.6,
      "predecessorTaskIds": [
        "6efb8f00-13c8-4ce4-91d2-ac0210a76df1"
      ],
      "status": "TODO",
      "priority": "P1",
      "assignee": "dev-2",
      "createTime": 1791690128015,
      "updateTime": 1791690128015,
      "es": 42.79,
      "ls": 42.79,
      "slack": 0
    },
    {
      "taskId": "e891e8a2-580b-43ae-93c0-aeeb7755b6c9",
      "taskName": "支付-数据库设计",
      "taskDesc": "规则拆解：支付 的数据库设计。来源需求：为电商中台开发【订单中心 v2.1】迭代：1) 完成订单创建、支付回调与库存扣减能力；2) 需对接第三方支付网关（外部接口，依赖对方文档与沙箱环境）；3) 支持",
      "taskType": "DB_DESIGN",
      "estimatedHours": 7.59,
      "predecessorTaskIds": [],
      "status": "TODO",
      "priority": "P0",
      "assignee": "dev-3",
      "createTime": 1791690128015,
      "updateTime": 1791690128015,
      "es": 0,
      "ls": 0,
      "slack": 0
    },
    {
      "taskId": "093f763c-6a7d-464a-ad5f-1d32eb5eaeb5",
      "taskName": "支付-接口开发",
      "taskDesc": "规则拆解：支付 的接口开发。来源需求：为电商中台开发【订单中心 v2.1】迭代：1) 完成订单创建、支付回调与库存扣减能力；2) 需对接第三方支付网关（外部接口，依赖对方文档与沙箱环境）；3) 支持",
      "taskType": "API_DEV",
      "estimatedHours": 13.2,
      "predecessorTaskIds": [
        "e891e8a2-580b-43ae-93c0-aeeb7755b6c9"
      ],
      "status": "TODO",
      "priority": "P0",
      "assignee": "dev-1",
      "createTime": 1791690128015,
      "updateTime": 1791690128015,
      "es": 7.59,
      "ls": 7.59,
      "slack": 0
    },
    {
      "taskId": "9feca953-a0e0-4bb5-8962-4b313f731f69",
      "taskName": "支付-业务代码实现",
      "taskDesc": "规则拆解：支付 的业务代码实现。来源需求：为电商中台开发【订单中心 v2.1】迭代：1) 完成订单创建、支付回调与库存扣减能力；2) 需对接第三方支付网关（外部接口，依赖对方文档与沙箱环境）；3) 支持",
      "taskType": "BUSINESS_CODE",
      "estimatedHours": 16,
      "predecessorTaskIds": [
        "093f763c-6a7d-464a-ad5f-1d32eb5eaeb5"
      ],
      "status": "TODO",
      "priority": "P1",
      "assignee": "dev-2",
      "createTime": 1791690128015,
      "updateTime": 1791690128015,
      "es": 20.79,
      "ls": 20.79,
      "slack": 0
    },
    {
      "taskId": "909fdf8e-ab24-47b5-bae2-922f057259ea",
      "taskName": "支付-联调与自测",
      "taskDesc": "规则拆解：支付 的联调与自测。来源需求：为电商中台开发【订单中心 v2.1】迭代：1) 完成订单创建、支付回调与库存扣减能力；2) 需对接第三方支付网关（外部接口，依赖对方文档与沙箱环境）；3) 支持",
      "taskType": "DEBUG",
      "estimatedHours": 6,
      "predecessorTaskIds": [
        "9feca953-a0e0-4bb5-8962-4b313f731f69"
      ],
      "status": "TODO",
      "priority": "P1",
      "assignee": "dev-3",
      "createTime": 1791690128015,
      "updateTime": 1791690128015,
      "es": 36.79,
      "ls": 36.79,
      "slack": 0
    },
    {
      "taskId": "f9df99f6-9fd9-427f-be3f-63391068961a",
      "taskName": "支付-功能测试",
      "taskDesc": "规则拆解：支付 的功能测试。来源需求：为电商中台开发【订单中心 v2.1】迭代：1) 完成订单创建、支付回调与库存扣减能力；2) 需对接第三方支付网关（外部接口，依赖对方文档与沙箱环境）；3) 支持",
      "taskType": "TEST",
      "estimatedHours": 9.6,
      "predecessorTaskIds": [
        "909fdf8e-ab24-47b5-bae2-922f057259ea"
      ],
      "status": "TODO",
      "priority": "P1",
      "assignee": "dev-1",
      "createTime": 1791690128015,
      "updateTime": 1791690128015,
      "es": 42.79,
      "ls": 42.79,
      "slack": 0
    },
    {
      "taskId": "902e476f-8bc7-4d3a-833c-a7f08e7e55ff",
      "taskName": "库存-数据库设计",
      "taskDesc": "规则拆解：库存 的数据库设计。来源需求：为电商中台开发【订单中心 v2.1】迭代：1) 完成订单创建、支付回调与库存扣减能力；2) 需对接第三方支付网关（外部接口，依赖对方文档与沙箱环境）；3) 支持",
      "taskType": "DB_DESIGN",
      "estimatedHours": 7.59,
      "predecessorTaskIds": [],
      "status": "TODO",
      "priority": "P0",
      "assignee": "dev-2",
      "createTime": 1791690128015,
      "updateTime": 1791690128015,
      "es": 0,
      "ls": 0,
      "slack": 0
    },
    {
      "taskId": "655fd155-d36b-4033-8797-af8c0b2614b9",
      "taskName": "库存-接口开发",
      "taskDesc": "规则拆解：库存 的接口开发。来源需求：为电商中台开发【订单中心 v2.1】迭代：1) 完成订单创建、支付回调与库存扣减能力；2) 需对接第三方支付网关（外部接口，依赖对方文档与沙箱环境）；3) 支持",
      "taskType": "API_DEV",
      "estimatedHours": 13.2,
      "predecessorTaskIds": [
        "902e476f-8bc7-4d3a-833c-a7f08e7e55ff"
      ],
      "status": "TODO",
      "priority": "P0",
      "assignee": "dev-3",
      "createTime": 1791690128015,
      "updateTime": 1791690128015,
      "es": 7.59,
      "ls": 7.59,
      "slack": 0
    },
    {
      "taskId": "0d7111c4-7562-49f9-b736-320310a86ccb",
      "taskName": "库存-业务代码实现",
      "taskDesc": "规则拆解：库存 的业务代码实现。来源需求：为电商中台开发【订单中心 v2.1】迭代：1) 完成订单创建、支付回调与库存扣减能力；2) 需对接第三方支付网关（外部接口，依赖对方文档与沙箱环境）；3) 支持",
      "taskType": "BUSINESS_CODE",
      "estimatedHours": 16,
      "predecessorTaskIds": [
        "655fd155-d36b-4033-8797-af8c0b2614b9"
      ],
      "status": "TODO",
      "priority": "P1",
      "assignee": "dev-1",
      "createTime": 1791690128015,
      "updateTime": 1791690128015,
      "es": 20.79,
      "ls": 20.79,
      "slack": 0
    },
    {
      "taskId": "bbfe7ccd-583d-4b96-94b1-f69cef88a23f",
      "taskName": "库存-联调与自测",
      "taskDesc": "规则拆解：库存 的联调与自测。来源需求：为电商中台开发【订单中心 v2.1】迭代：1) 完成订单创建、支付回调与库存扣减能力；2) 需对接第三方支付网关（外部接口，依赖对方文档与沙箱环境）；3) 支持",
      "taskType": "DEBUG",
      "estimatedHours": 6,
      "predecessorTaskIds": [
        "0d7111c4-7562-49f9-b736-320310a86ccb"
      ],
      "status": "TODO",
      "priority": "P1",
      "assignee": "dev-2",
      "createTime": 1791690128015,
      "updateTime": 1791690128015,
      "es": 36.79,
      "ls": 36.79,
      "slack": 0
    },
    {
      "taskId": "a36bf3a9-64d8-49d8-bbf9-d2ea3637c8a2",
      "taskName": "库存-功能测试",
      "taskDesc": "规则拆解：库存 的功能测试。来源需求：为电商中台开发【订单中心 v2.1】迭代：1) 完成订单创建、支付回调与库存扣减能力；2) 需对接第三方支付网关（外部接口，依赖对方文档与沙箱环境）；3) 支持",
      "taskType": "TEST",
      "estimatedHours": 9.6,
      "predecessorTaskIds": [
        "bbfe7ccd-583d-4b96-94b1-f69cef88a23f"
      ],
      "status": "TODO",
      "priority": "P1",
      "assignee": "dev-3",
      "createTime": 1791690128015,
      "updateTime": 1791690128015,
      "es": 42.79,
      "ls": 42.79,
      "slack": 0
    },
    {
      "taskId": "c3743848-c16a-46a5-a58f-fe78521082cc",
      "taskName": "版本部署与发布",
      "taskDesc": "汇总全部模块，执行部署与发布（依赖所有测试任务）",
      "taskType": "DEPLOY",
      "estimatedHours": 5.28,
      "predecessorTaskIds": [
        "6a21a9d4-f74b-4dec-b293-c5f5bdba9363",
        "f9df99f6-9fd9-427f-be3f-63391068961a",
        "a36bf3a9-64d8-49d8-bbf9-d2ea3637c8a2"
      ],
      "status": "TODO",
      "priority": "P0",
      "assignee": "dev-1",
      "createTime": 1791690128015,
      "updateTime": 1791690128015,
      "es": 52.39,
      "ls": 52.39,
      "slack": 0
    },
    {
      "taskId": "68301a39-c35d-4cae-9fdf-4fa5540d6635",
      "taskName": "发布文档与交付说明",
      "taskDesc": "整理接口文档、部署说明与交付物（依赖全部开发任务）",
      "taskType": "DOC",
      "estimatedHours": 4,
      "predecessorTaskIds": [
        "d83dcbd0-7f58-4be7-87b4-8dab69a88362",
        "63e1b52b-301c-4ee9-8e67-76f7689c57fb",
        "093f763c-6a7d-464a-ad5f-1d32eb5eaeb5",
        "9feca953-a0e0-4bb5-8962-4b313f731f69",
        "655fd155-d36b-4033-8797-af8c0b2614b9",
        "0d7111c4-7562-49f9-b736-320310a86ccb"
      ],
      "status": "TODO",
      "priority": "P3",
      "assignee": "dev-2",
      "createTime": 1791690128015,
      "updateTime": 1791690128015,
      "es": 36.79,
      "ls": 53.67,
      "slack": 16.880000000000003
    }
  ],
  "riskList": [
    {
      "riskId": "RISK-3d913df9",
      "level": "MEDIUM",
      "taskIds": [
        "9feca953-a0e0-4bb5-8962-4b313f731f69",
        "bbfe7ccd-583d-4b96-94b1-f69cef88a23f",
        "68301a39-c35d-4cae-9fdf-4fa5540d6635",
        "6a21a9d4-f74b-4dec-b293-c5f5bdba9363"
      ],
      "description": "开发人员「dev-2」存在 2 天单日工时超限，峰值 11.21h（2026-10-16）",
      "suggest": "拆分任务、调整任务并行度或增加人手，确保单人单日总工时不超过 8 小时"
    },
    {
      "riskId": "RISK-7b7a7b9a",
      "level": "MEDIUM",
      "taskIds": [
        "d5411625-5bbb-42e3-b1b3-4dd30d594ce6",
        "d83dcbd0-7f58-4be7-87b4-8dab69a88362",
        "63e1b52b-301c-4ee9-8e67-76f7689c57fb",
        "6efb8f00-13c8-4ce4-91d2-ac0210a76df1",
        "6a21a9d4-f74b-4dec-b293-c5f5bdba9363",
        "e891e8a2-580b-43ae-93c0-aeeb7755b6c9",
        "093f763c-6a7d-464a-ad5f-1d32eb5eaeb5",
        "9feca953-a0e0-4bb5-8962-4b313f731f69",
        "909fdf8e-ab24-47b5-bae2-922f057259ea",
        "f9df99f6-9fd9-427f-be3f-63391068961a",
        "902e476f-8bc7-4d3a-833c-a7f08e7e55ff",
        "655fd155-d36b-4033-8797-af8c0b2614b9",
        "0d7111c4-7562-49f9-b736-320310a86ccb",
        "bbfe7ccd-583d-4b96-94b1-f69cef88a23f",
        "a36bf3a9-64d8-49d8-bbf9-d2ea3637c8a2"
      ],
      "description": "需求涉及第三方/外部接口对接，外部依赖可能未就绪",
      "suggest": "提前与第三方确认接口文档、联调窗口与沙箱环境，必要时准备 Mock 降级方案"
    },
    {
      "riskId": "RISK-cf187bb1",
      "level": "MEDIUM",
      "taskIds": [],
      "description": "需求可能需要专用测试/联调环境，环境搭建存在不确定性",
      "suggest": "提前申请或搭建测试环境，纳入迭代前置任务并预留缓冲"
    },
    {
      "riskId": "RISK-50af1663",
      "level": "MEDIUM",
      "taskIds": [],
      "description": "需求描述存在模糊表述，任务边界可能与预期不一致",
      "suggest": "在需求评审阶段明确验收标准与边界条件，避免开发中返工"
    }
  ],
  "criticalPath": [
    "d5411625-5bbb-42e3-b1b3-4dd30d594ce6",
    "d83dcbd0-7f58-4be7-87b4-8dab69a88362",
    "63e1b52b-301c-4ee9-8e67-76f7689c57fb",
    "6efb8f00-13c8-4ce4-91d2-ac0210a76df1",
    "6a21a9d4-f74b-4dec-b293-c5f5bdba9363",
    "c3743848-c16a-46a5-a58f-fe78521082cc"
  ],
  "burndownRawData": [
    {
      "time": "2026-10-12",
      "remainingHours": 166.45
    },
    {
      "time": "2026-10-13",
      "remainingHours": 145.64
    },
    {
      "time": "2026-10-14",
      "remainingHours": 124.84
    },
    {
      "time": "2026-10-15",
      "remainingHours": 104.03
    },
    {
      "time": "2026-10-16",
      "remainingHours": 83.23
    },
    {
      "time": "2026-10-19",
      "remainingHours": 62.42
    },
    {
      "time": "2026-10-20",
      "remainingHours": 41.61
    },
    {
      "time": "2026-10-21",
      "remainingHours": 20.81
    },
    {
      "time": "2026-10-22",
      "remainingHours": 0
    }
  ],
  "totalEstimatedHours": 166.45,
  "totalDurationHours": 57.67
}

--- STEP 2 · report_generate：团队可读文本报告 ---
# 迭代规划报告

- 计划 ID：20827101-c31d-480f-b43f-95905404c789
- 迭代窗口：2026-10-12 ~ 2026-10-30
- 开发人数：3
- 原始需求：为电商中台开发【订单中心 v2.1】迭代：1) 完成订单创建、支付回调与库存扣减能力；2) 需对接第三方支付网关（外部接口，依赖对方文档与沙箱环境）；3) 支持订单查询与报表统计；4) 优化下单链路性能，并搭建压测环境；5) 产出接口文档与部署说明。

## 一、概览

- 任务总数：17
- 项目总预估工时：166.45h
- 关键路径总工期：57.67h（约 8 个工作日）
- 风险条目：4（HIGH 0）

## 二、关键路径

1. 订单-数据库设计（DB_DESIGN，7.59h）
2. 订单-接口开发（API_DEV，13.2h）
3. 订单-业务代码实现（BUSINESS_CODE，16h）
4. 订单-联调与自测（DEBUG，6h）
5. 订单-功能测试（TEST，9.6h）
6. 版本部署与发布（DEPLOY，5.28h）

## 三、任务清单

序号 | 任务 | 类型 | 优先级 | 工时 | 负责人 | 前置 | 状态 | 时间窗
---|---|---|---|---|---|---|---|---
1 | 订单-数据库设计 | DB_DESIGN | P0 | 7.59h | dev-1 | — | 待办 | es=0 | ls=0 | slack=0
2 | 订单-接口开发 | API_DEV | P0 | 13.2h | dev-2 | 订单-数据库设计 | 待办 | es=7.59 | ls=7.59 | slack=0
3 | 订单-业务代码实现 | BUSINESS_CODE | P1 | 16h | dev-3 | 订单-接口开发 | 待办 | es=20.79 | ls=20.79 | slack=0
4 | 订单-联调与自测 | DEBUG | P1 | 6h | dev-1 | 订单-业务代码实现 | 待办 | es=36.79 | ls=36.79 | slack=0
5 | 订单-功能测试 | TEST | P1 | 9.6h | dev-2 | 订单-联调与自测 | 待办 | es=42.79 | ls=42.79 | slack=0
6 | 支付-数据库设计 | DB_DESIGN | P0 | 7.59h | dev-3 | — | 待办 | es=0 | ls=0 | slack=0
7 | 支付-接口开发 | API_DEV | P0 | 13.2h | dev-1 | 支付-数据库设计 | 待办 | es=7.59 | ls=7.59 | slack=0
8 | 支付-业务代码实现 | BUSINESS_CODE | P1 | 16h | dev-2 | 支付-接口开发 | 待办 | es=20.79 | ls=20.79 | slack=0
9 | 支付-联调与自测 | DEBUG | P1 | 6h | dev-3 | 支付-业务代码实现 | 待办 | es=36.79 | ls=36.79 | slack=0
10 | 支付-功能测试 | TEST | P1 | 9.6h | dev-1 | 支付-联调与自测 | 待办 | es=42.79 | ls=42.79 | slack=0
11 | 库存-数据库设计 | DB_DESIGN | P0 | 7.59h | dev-2 | — | 待办 | es=0 | ls=0 | slack=0
12 | 库存-接口开发 | API_DEV | P0 | 13.2h | dev-3 | 库存-数据库设计 | 待办 | es=7.59 | ls=7.59 | slack=0
13 | 库存-业务代码实现 | BUSINESS_CODE | P1 | 16h | dev-1 | 库存-接口开发 | 待办 | es=20.79 | ls=20.79 | slack=0
14 | 库存-联调与自测 | DEBUG | P1 | 6h | dev-2 | 库存-业务代码实现 | 待办 | es=36.79 | ls=36.79 | slack=0
15 | 库存-功能测试 | TEST | P1 | 9.6h | dev-3 | 库存-联调与自测 | 待办 | es=42.79 | ls=42.79 | slack=0
16 | 版本部署与发布 | DEPLOY | P0 | 5.28h | dev-1 | 订单-功能测试、支付-功能测试、库存-功能测试 | 待办 | es=52.39 | ls=52.39 | slack=0
17 | 发布文档与交付说明 | DOC | P3 | 4h | dev-2 | 订单-接口开发、订单-业务代码实现、支付-接口开发、支付-业务代码实现、库存-接口开发、库存-业务代码实现 | 待办 | es=36.79 | ls=53.67 | slack=16.880000000000003

## 四、风险清单

- 【MEDIUM】 开发人员「dev-2」存在 2 天单日工时超限，峰值 11.21h（2026-10-16）
  - 关联任务：支付-业务代码实现、库存-联调与自测、发布文档与交付说明、订单-功能测试
  - 整改建议：拆分任务、调整任务并行度或增加人手，确保单人单日总工时不超过 8 小时
- 【MEDIUM】 需求涉及第三方/外部接口对接，外部依赖可能未就绪
  - 关联任务：订单-数据库设计、订单-接口开发、订单-业务代码实现、订单-联调与自测、订单-功能测试、支付-数据库设计、支付-接口开发、支付-业务代码实现、支付-联调与自测、支付-功能测试、库存-数据库设计、库存-接口开发、库存-业务代码实现、库存-联调与自测、库存-功能测试
  - 整改建议：提前与第三方确认接口文档、联调窗口与沙箱环境，必要时准备 Mock 降级方案
- 【MEDIUM】 需求可能需要专用测试/联调环境，环境搭建存在不确定性
  - 关联任务：—
  - 整改建议：提前申请或搭建测试环境，纳入迭代前置任务并预留缓冲
- 【MEDIUM】 需求描述存在模糊表述，任务边界可能与预期不一致
  - 关联任务：—
  - 整改建议：在需求评审阶段明确验收标准与边界条件，避免开发中返工

## 五、燃尽图原始数据

日期 | 剩余工时
---|---
2026-10-12 | 166.45h
2026-10-13 | 145.64h
2026-10-14 | 124.84h
2026-10-15 | 104.03h
2026-10-16 | 83.23h
2026-10-19 | 62.42h
2026-10-20 | 41.61h
2026-10-21 | 20.81h
2026-10-22 | 0h

--- STEP 3 · task_update：把关键路径任务标记为 DELAYED，自动触发 replan_all ---
目标关键任务：订单-数据库设计（d5411625-5bbb-42e3-b1b3-4dd30d594ce6）→ status=DELAYED
重规划后总工期：57.67h；风险条目：5
已高亮 HIGH 风险：关键路径上存在 DELAYED 任务：订单-数据库设计

--- STEP 4 · 构造循环依赖：验证 dag_check 阻断 + 报错可读性 ---
新增任务：循环验证任务（e3a47c60-6fac-4a9f-80ef-7cfda540dc35）依赖 68301a39-c35d-4cae-9fdf-4fa5540d6635
现在把 68301a39-c35d-4cae-9fdf-4fa5540d6635 的前置改为 e3a47c60-6fac-4a9f-80ef-7cfda540dc35，构成 A→B→A 环……
已按预期阻断：检测到循环依赖，无法继续规划。环内任务：68301a39-c35d-4cae-9fdf-4fa5540d6635、e3a47c60-6fac-4a9f-80ef-7cfda540dc35
环内任务：68301a39-c35d-4cae-9fdf-4fa5540d6635、e3a47c60-6fac-4a9f-80ef-7cfda540dc35
通过 task_delete 移除新增任务以解除环，验证自动恢复：
恢复后总工期：57.67h；风险条目：6

==============================================================================
# Demo 结束
==============================================================================
审计日志（含每次工具调用的入参/出参/耗时）已输出到 stderr。

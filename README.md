# dsh-plugin-iteration-planner

> 企业级研发迭代规划智能 Agent —— 基于 **DeepSeek Harness (dsh)** / **Cordis** 的独立插件。
> 输入业务需求/版本开发需求，输出：**原子研发任务清单 + 任务依赖 DAG + 关键路径 + 迭代排期 + 风险报告 + 迭代报表 + 燃尽图原始数据**，并支持动态任务变更后的**全量重规划**。

本项目严格遵守「一切皆插件」约束：**未改动 Harness / Cordis 任何内核源码**，全部业务逻辑封装在本插件内。

---

## 一、快速开始

```bash
# 1) 环境要求：Node.js >= 22（开发环境实测 v22.16.0 / npm 10.9.2）
node -v

# 2) 安装依赖
npm install

# 3) 类型检查（tsc --noEmit）
npm run typecheck

# 4) 单元测试（vitest）
npm test

# 5) 运行本地 Demo（完整流程 + 样例输出）
npm run demo

# 6) 安装并启动企业控制台前端（可选，详见 §九）
npm run web:install && npm run web:dev
```

`npm run demo` 会直接在终端打印完整流程结果；重定向即可生成样例文件：

```powershell
# 标准输出 = 业务结果；stderr = 审计日志（每次工具调用的入参/出参/耗时）
npx tsx demo/demo.ts 1> examples/sample-output.md 2> demo-stderr.txt
```

> 完整演示输出见 [`examples/sample-output.md`](./examples/sample-output.md)。

---

## 二、目录结构与分层

分层严格遵循需求 §七.2「工具定义层 / DAG 算法层 / 业务服务层 / 模型实体层 / Agent 编排层」：

```
dsh-plugin-iteration-planner/
├── src/
│   ├── index.ts                 # 【插件入口】Cordis 插件：name / inject / apply
│   ├── constants/
│   │   └── enums.ts             # 【常量枚举层】全部枚举 + 缓冲规则 + 阈值（禁止硬编码散落）
│   ├── models/
│   │   └── entities.ts          # 【模型实体层】Task / ProjectPlan / RiskItem / BurndownItem
│   ├── algorithm/
│   │   ├── dag.ts               # 【DAG 算法层】Kahn 拓扑排序 + 环检测 + 孤立任务
│   │   ├── aoe.ts               # 【DAG 算法层】AOE 网关键路径（es/ls/slack）
│   │   ├── burndown.ts          # 【DAG 算法层】燃尽图原始数据
│   │   ├── workday.ts           # 【DAG 算法层】工作日历（跳过周末）
│   │   └── index.ts             # 算法统一出口
│   ├── service/
│   │   ├── planner-service.ts   # 【业务服务层】PlannerService 编排；PlanCycleError / PlanStateError
│   │   ├── buffer.ts            # 工时评估修正规则（§3.3）
│   │   ├── risk.ts              # 风险检测规则（§3.4）
│   │   ├── calendar.ts          # 排期日历 / 人员负载
│   │   └── report.ts            # 迭代报表生成（§四.10）
│   ├── llm/
│   │   ├── llm-decomposer.ts    # 【LLM 策略】Harness LLM 主路径（ctx.llm 适配）
│   │   ├── rule-decomposer.ts   # 【规则降级】离线可复现拆解实现
│   │   ├── json.ts              # LLM 输出 JSON 容错解析
│   │   └── types.ts             # LlmClient 接口（可替换）
│   ├── tools/
│   │   ├── index.ts             # 工具统一注册出口（registerAllTools）
│   │   ├── schemas.ts           # 工具入参 schema DSL
│   │   ├── planning-tools.ts    # task_decompose / plan_iteration / replan_all
│   │   ├── dag-tools.ts         # dag_build / dag_check / schedule_calc
│   │   ├── task-tools.ts        # task_add / task_update / task_delete
│   │   ├── risk-tools.ts        # risk_scan
│   │   └── report-tools.ts      # report_generate
│   ├── prompt/
│   │   └── system-prompt.ts     # 【Agent 编排层】角色提示词（§六）+ ctx.systemPrompt.section 注入
│   ├── logger/
│   │   └── audit.ts             # 全链路审计日志（入参/出参/耗时）
│   ├── extensions/
│   │   └── interfaces.ts        # 预留扩展接口（Jira / Git / CI-CD），仅接口不限死实现
│   ├── repository/              # 【持久化层】仓储接口 + 内存适配器 + PostgreSQL 适配器
│   │   ├── types.ts             # PlanRepository / AuditRepository / RepositoryBundle 接口
│   │   ├── memory.ts            # 内存适配器（单测与演示用，零外部依赖）
│   │   ├── postgres.ts          # PostgreSQL 适配器（共享连接池 + 幂等迁移）
│   │   ├── migrations.ts        # TS 内嵌 SQL 迁移（规避构建期文件复制问题）
│   │   └── index.ts             # createRepository 工厂（memory | postgres）
│   └── api/                     # 【API 层】Fastify 服务（REST + WebSocket + Swagger + 鉴权）
│       ├── standalone.ts        # 独立启动入口（Docker / 裸机，自动加载 .env）
│       ├── plugin.ts            # Cordis 插件封装（在 Harness 内托管 HTTP 生命周期）
│       ├── server.ts            # Fastify 装配（CORS / 静态 SPA / WS / 统一错误处理）
│       ├── routes.ts            # REST 路由（计划 / 任务 / DAG / 报表 / 审计）
│       ├── plan-session.ts      # 多计划会话隔离 + 懒加载恢复
│       ├── validation.ts        # zod 入参校验（字段级错误明细）
│       ├── graph.ts             # DAG 视图数据组装
│       ├── events.ts            # 计划事件总线（驱动 WebSocket 推送）
│       ├── audit-bridge.ts      # 审计日志落库桥接
│       ├── errors.ts            # 异常 → HTTP 状态码 / 稳定错误码映射
│       ├── llm-client.ts        # OpenAI 兼容 LLM 客户端
│       └── config.ts            # 环境变量配置加载
├── python/                      # 【Python 对外服务】FastAPI 网关 + LLM Agent 编排（不承载算法）
│   ├── app/
│   │   ├── main.py              # FastAPI 装配（CORS / 鉴权 / 异常信封 / 路由注册）
│   │   ├── config.py            # 环境变量配置（pydantic-settings）
│   │   ├── engine_client.py     # 调用内部 TS 引擎（信封解包 / 错误码映射 / 原样透传）
│   │   ├── errors.py            # 统一错误信封（与 TS 侧完全一致）
│   │   ├── models.py            # Agent 原生端点请求 / 响应模型
│   │   ├── state.py             # 运行时上下文（配置 / 引擎 / Agent / WS）
│   │   ├── ws.py                # WebSocket 广播 + 引擎事件流桥接
│   │   ├── agent/               # 【Agent 编排层】提示词 / 工具定义与调度 / LLM 客户端 / function calling 循环
│   │   └── routes/              # 系统路由 / Agent 路由 / 网关透传（SPA 托管）
│   ├── tests/                   # pytest（Mock 引擎 + 脚本化 LLM，覆盖转发/错误/鉴权/Agent）
│   ├── requirements.txt         # 运行时依赖
│   ├── requirements-dev.txt     # 开发测试依赖
│   ├── pyproject.toml           # pytest 配置
│   └── run.py                   # 启动入口（uvicorn，读取 .env）
├── test/
│   ├── dag.test.ts              # Kahn 环检测（正常/环/孤立/悬空依赖）
│   ├── aoe.test.ts              # AOE 关键路径（链/并行/孤立/空/环）
│   ├── buffer.test.ts           # 缓冲修正规则
│   ├── risk.test.ts             # 风险检测硬编码规则
│   ├── workday.test.ts          # 工作日历
│   ├── api.test.ts              # API 集成测试（fastify.inject + 内存仓储，15 项）
│   └── fixtures.ts              # 测试构造器
├── demo/
│   └── demo.ts                  # 【本地 Demo 入口】样例需求 → 完整流程输出
├── examples/
│   └── sample-output.md         # Demo 完整输出样例（交付物 §8.6）
├── web/                         # 【企业控制台】Vue3 + Vite + Element Plus + ECharts + AntV X6
│   ├── src/
│   │   ├── api/                 # 前端 API 客户端 + 领域类型契约
│   │   ├── stores/              # Pinia 状态（计划 / 偏好设置）
│   │   ├── views/               # 6 个页面（列表 / 新建 / 总览 / 任务 / DAG / 审计）
│   │   ├── components/          # KPI 卡片 / 燃尽图 / 风险面板 / 任务编辑弹窗
│   │   ├── layouts/             # 控制台主布局（导航 + 计划切换 + WS 状态）
│   │   └── utils/               # 枚举文案映射与格式化
│   └── vite.config.ts
├── deploy/
│   └── nginx.conf               # 可选：Nginx 反向代理 / TLS 网关示例
├── Dockerfile                   # 多阶段构建（前端构建 → 一体化 TS 运行时）
├── Dockerfile.python            # 多阶段构建（前端构建 → Python 对外服务运行时）
├── docker-compose.yml           # postgres + engine(TS,内网) + python(对外) 编排
├── .env.example                 # 环境变量样例
├── cordis.patch.yml             # bundle 挂载补丁（package.json 的 dsh.bundle 指向它）
├── cordis.yml                   # 本地开发 / overlay 补丁示例
├── package.json                 # 含 dsh.bundle 声明
└── tsconfig.json
```

---

## 三、接入 DeepSeek Harness

插件以标准 Cordis 插件形态加载：导出 `name` / `inject` / `apply(ctx, config)`，通过 `ctx.tools.register` 注册工具、通过 `ctx.systemPrompt.section` 注入角色提示词。**卸载时由其可逆副作用自动清理，无需改动内核。**

### 3.1 方式一：作为 bundle 安装（推荐，生产接入）

`package.json` 已声明：

```json
"dsh": { "bundle": "./cordis.patch.yml" }
```

`cordis.patch.yml` 向组合树插入一行插件：

```yaml
- insert:
    - id: iteration-planner
      name: dsh-plugin-iteration-planner
      config:
        fallbackToRule: true
        workHoursPerDay: 8
        overloadThreshold: 8
        # llmProvider: deepseek
        # llmModel: deepseek-chat
```

在目标 profile 的 `dsh.profile.bundles` 中加入本包名，dsh 会在「空根」之上依次叠加各 bundle 补丁，从而把本插件行插入组合树。上层 profile 的 `cordis.patch.yml`、`$DSH_HOME/cordis.patch.yml`、`--patch` 均可再按 `id: iteration-planner` 覆盖该行 config。

### 3.2 方式二：本地开发直接从源码挂载（overlay demo）

```bash
# Windows 建议使用正斜杠绝对路径；模块说明符按运行时工作目录解析
dsh web --patch "E:/go/dsh-plugin-iteration-planner/cordis.yml"
```

`cordis.yml` 中「形式 B」即为源码直接挂载示例（把 `name` 改为 `src/index.ts` 的绝对路径）。dsh 支持直接加载 `.ts` 插件源码。

### 3.3 验证配置已生效

```bash
dsh --profile <你的 profile 名> --dump-config
# 在输出中应能看到 id 为 iteration-planner 的行及其 config
```

### 3.4 启用 LLM 主路径

默认 `fallbackToRule: true`，**未配置 LLM 时全程使用规则实现，离线可运行、结果可复现**。启用 LLM 只需在 config 中补齐（要求 profile 已挂载模型提供方）：

```yaml
config:
  llmProvider: deepseek
  llmModel: deepseek-chat
```

LLM 失败或输出格式不合法时会自动降级到规则实现并记录审计日志（见 §5 异常处理）。

### 3.5 插件配置项（PluginConfig）

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `llm` | `LlmClient` | — | 自定义 LLM 客户端（优先级最高） |
| `llmProvider` | `string` | — | Harness LLM provider，配合 `ctx.llm` 使用 |
| `llmModel` | `string` | — | Harness LLM model |
| `fallbackToRule` | `boolean` | `true` | LLM 失败是否降级到规则实现 |
| `workHoursPerDay` | `number` | `8` | 单开发人单日工时（人员过载校验） |
| `overloadThreshold` | `number` | `= workHoursPerDay` | 过载阈值（小时） |
| `bufferRules` | `BufferRule[]` | 内置 §3.3 | 缓冲规则覆盖 |
| `bufferApplyOrder` | `('type'\|'priority')[]` | `['type','priority']` | 缓冲叠加顺序 |
| `extensions` | `PlannerExtensions` | — | 外部系统扩展点（Jira / Git / CI-CD） |
| `auditSink` | `(line) => void` | `ctx.logger` | 审计输出目标 |
| `systemPrompt` | `string` | 内置 §六 | 角色提示词覆盖 |

---

## 四、Function Calling 工具清单

共注册 **11 个**工具：需求规定的 10 个规范工具 + 1 个 `plan_iteration` 编排入口。

| # | 工具 | 作用 | 入参 → 出参 |
|---|---|---|---|
| 1 | `task_decompose` | LLM 将大需求拆解为原子研发任务 | `requirement, devCount, deadline` → `Task[]`（仅基础字段） |
| 2 | `dag_build` | 构建 DAG 图结构 | `taskList` → `{adjList, inDegree}` |
| 3 | `dag_check` | Kahn 算法检测环并输出拓扑序 | `adjList, inDegree` → `{hasCycle, cycleNodes, topoOrder}` |
| 4 | `schedule_calc` | AOE 计算关键路径与时间窗口 | `taskList, topoOrder` → `{criticalPath, esMap, lsMap, totalDurationHours}` |
| 5 | `task_add` | 新增任务到任务集合 | `newTask` → 更新后的 `taskList` |
| 6 | `task_update` | 修改任务字段 | `taskId, updateField, newValue` → 更新后的 `Task` |
| 7 | `task_delete` | 删除任务并清理其他任务的依赖引用 | `taskId` → 更新后的 `taskList` |
| 8 | `risk_scan` | 硬编码规则 + LLM 语义风险识别 | `taskList, scheduleResult` → `RiskItem[]` |
| 9 | `replan_all` | 触发全量重规划 | `taskList, globalConstraints` → 完整 `ProjectPlan` |
| 10 | `report_generate` | 生成迭代报表 + 燃尽原始数据 | `projectPlan` → 报表文本 + 燃尽 JSON |
| 11 | `plan_iteration` | **编排入口**：一次调用跑完拆解→缓冲→DAG→AOE→风险→燃尽→报表 | `requirement, devMemberCount, iterationDeadline, iterationStartDate` → `ProjectPlan` |

---

## 五、核心算法与实现要点

### 5.1 Kahn 拓扑排序与环检测（[`src/algorithm/dag.ts`](./src/algorithm/dag.ts)）

构建邻接表 `adjList` 与入度数组 `inDegree` → 入度 0 入队 → 逐个出队并递减后置入度 → 若出队数 < 任务总数则 `hasCycle=true`，并输出环内未入队节点。输出拓扑序。

### 5.2 AOE 网关键路径（[`src/algorithm/aoe.ts`](./src/algorithm/aoe.ts)）

正向遍历拓扑序求最早开始 `es`；反向遍历逆拓扑序求最晚开始 `ls`；`slack = ls - es`；`slack≈0` 为关键任务；回溯最长路径得到有序 `criticalPath`。

**⚠ 两处必要口径修正（已与需求方确认，建议知悉）**

| 项 | 需求原文 | 本项目实现 | 原因 |
|---|---|---|---|
| 项目总工期 | 关键路径上所有任务工时之和 | `totalDurationHours = max(es + 工时)` | 活动在顶点（AOV）语义下，多条并行关键分支时「工时之和」会重复计工；`max(es+工时)` 才等于真实工期 |
| 汇点 `ls` | `ls = 项目总工期` | `ls = 项目总工期 − 自身工时` | `ls` 定义是「最晚**开始**时间」；汇点须在最晚于总工期时刻**完成**，故最晚开始 = 总工期 − 自身工时，否则 `slack = ls - es` 恒等于自身工时，汇点永远无法判为关键任务、关键路径必然缺尾 |

> 除以上两处（及下述 bug 修复）外，算法语义、字段、流程均严格遵循需求文档。

### 5.3 工时评估修正规则（[`src/service/buffer.ts`](./src/service/buffer.ts)，§3.3）

- P0 任务：工时上浮 **10%**
- TEST / DEPLOY 任务：统一增加 **20%** 缓冲工时
- DB_DESIGN 任务：统一增加 **15%** 缓冲
- 多人并行：同一开发人单日总工时 **> 8h** 标记人员过载风险

叠加顺序固定为 `type → priority`（乘法交换律下结果稳定），缓冲明细可通过 `service.getBufferAdjustments()` 获取。

### 5.4 风险检测（[`src/service/risk.ts`](./src/service/risk.ts)，§3.4）

硬编码规则：
1. **循环依赖** → `HIGH`，输出环内任务，建议修正依赖关系
2. **人员过载**（单人单日 > 8h）→ `MEDIUM`，建议拆分任务或增加人手
3. **关键路径延期**（关键路径任务为 `DELAYED`）→ `HIGH`，建议调整排期或加人
4. **孤立任务**（既无前置也无后置）→ `LOW`，提示检查依赖是否遗漏

LLM 语义扫描：识别需求边界模糊、第三方接口依赖未就绪、测试环境未搭建等非结构化风险，等级按影响范围自动判定。

### 5.5 动态重规划流程（[`PlannerService.computePlan`](./src/service/planner-service.ts)，§3.5）

触发场景：新增 / 删除任务、修改工时 / 前置依赖 / 状态 / 迭代截止时间。执行顺序**严格固定**：

1. 更新内存中的任务集合
2. `dag_check` Kahn 拓扑校验 —— **存在环立即抛出 `PlanCycleError` 并终止后续步骤**
3. `schedule_calc` 重算 `es/ls/slack`、关键路径、总工期
4. `risk_scan` 全量风险扫描
5. 重新生成燃尽数据
6. 输出更新后的完整 `ProjectPlan`

---

## 六、异常处理与日志规范

**异常处理（§8）** —— 全部失败路径均有明确错误提示，绝不静默崩溃：

| 异常 | 触发场景 | 行为 |
|---|---|---|
| `PlanCycleError` | DAG 存在环 | 抛出，`message` 提示修正依赖，`cycleNodes` 给出环内任务；**阻断后续流程** |
| `PlanStateError` | 引用不存在的 `taskId`、非法字段更新等 | 抛出并说明原因 |
| LLM 输出格式错误 | JSON 解析失败/字段缺失 | 记录审计日志 → 按 `fallbackToRule` 降级规则实现 |

**全链路审计日志（[`src/logger/audit.ts`](./src/logger/audit.ts)，§7.5）** —— 每次工具调用、DAG 变更、重规划动作都记录一行审计，含 `action / ok / durationMs / 入参 / 出参`。默认输出到 `ctx.logger`，可经 `auditSink` 重定向。

**扩展接口（§7.6）** —— [`src/extensions/interfaces.ts`](./src/extensions/interfaces.ts) 预留 Jira / Git / CI-CD 对接接口，当前仅接口定义，**不写死任何外部依赖**。

---

## 七、测试

```bash
npm test          # vitest run：6 个测试文件 / 48 个用例（33 算法与规则 + 15 API 集成）
npm run typecheck # tsc --noEmit，严格模式（strict + noUnusedLocals/Parameters）
```

| 测试文件 | 覆盖场景 |
|---|---|
| [`test/dag.test.ts`](./test/dag.test.ts) | 线性链、分叉汇聚、`A→B→C→A` 环、环+环外节点、孤立节点、链中节点不误判孤立、悬空依赖 |
| [`test/aoe.test.ts`](./test/aoe.test.ts) | 空任务集、线性链全链关键、并行分支取最长、孤立节点取最长、存在环 `incomplete=true` |
| [`test/buffer.test.ts`](./test/buffer.test.ts) | P0/TEST/DEPLOY/DB_DESIGN 单项与叠加、未命中不变、纯函数不改入参、批量 diff |
| [`test/risk.test.ts`](./test/risk.test.ts) | 无风险链、循环依赖 HIGH、人员过载 MEDIUM、关键路径 DELAYED HIGH、孤立任务 LOW、组合场景 |
| [`test/workday.test.ts`](./test/workday.test.ts) | 日期往返、`todayISO`、周末判定、`addWorkdays` 跳周末、周末起点归位、工作日枚举 |
| [`test/api.test.ts`](./test/api.test.ts) | 健康检查、元数据、计划生命周期、DAG 视图、燃尽、报表导出、任务增删改、重规划、循环依赖 409、入参校验 400、鉴权 401 |

---

## 八、演示样例

Mock 输入（`demo/demo.ts`）：

- **需求**：为电商中台开发【订单中心 v2.1】迭代：1) 完成订单创建、支付回调与库存扣减能力；2) 需对接第三方支付网关；3) 支持订单查询与报表统计；4) 优化下单链路性能并搭建压测环境；5) 产出接口文档与部署说明。
- **迭代窗口**：`2026-10-12 ~ 2026-10-30`；**可用开发人数**：3

Demo 依次演示：

1. `STEP 0` 插件注册的 11 个工具清单
2. `STEP 1` `plan_iteration` 完整规划（结构化摘要 + 缓冲明细 + 风险清单 + 燃尽数据 + 完整 `ProjectPlan` JSON）
3. `STEP 2` `report_generate` 团队可读文本报表
4. `STEP 3` 将关键路径任务标记 `DELAYED` → 自动 `replan_all` → 高亮 **HIGH** 风险
5. `STEP 4` 构造 `A→B→A` 环 → 验证 `dag_check` 阻断与报错可读性 → `task_delete` 解除环并恢复

**样例关键结果**：

| 指标 | 值 |
|---|---|
| 任务数 | 17 |
| 总预估工时 | 166.45h |
| 总工期（关键路径） | 57.67h |
| 关键路径 | 订单-数据库设计 → 订单-接口开发 → 订单-业务代码实现 → 订单-联调与自测 → 订单-功能测试 → 版本部署与发布 |
| 风险 | MEDIUM × 4（人员过载 / 第三方依赖 / 测试环境 / 需求模糊） |
| STEP 3 后 | 新增 HIGH 风险「关键路径上存在 DELAYED 任务」 |
| STEP 4 | 环内 2 个任务被正确识别并阻断 |

完整输出见 [`examples/sample-output.md`](./examples/sample-output.md)。

---

## 九、企业控制台与部署

除作为 dsh 插件嵌入 Harness 外，本项目额外提供**可独立落地的企业级控制台**：
一套 Vue3 SPA + Python 对外服务（FastAPI）+ 内部算法引擎（Fastify）。三者共用同一份算法层与服务层，**算法与服务代码零改动**。

### 10.1 架构与数据流

```
浏览器 (Vue3 SPA)
  │  REST /api/**            WebSocket /ws（计划变更实时推送）
  ▼
Python 对外服务（FastAPI 网关 + LLM Agent 编排）  ← 唯一对外入口，统一鉴权
  │  ├── /api/**         原样透传（信封 / 状态码 / 导出文件零改动）
  │  └── /api/agent/**   LLM function calling（自然语言 → 迭代计划）
  ▼
内部算法引擎：Fastify API 服务 ──► PlanSessionManager（多计划隔离、懒加载恢复）
  │                       │
  │                       ▼
  │                 PlannerService（既有服务层：Kahn → AOE → 风险 → 燃尽）
  │                       │
  ▼                       ▼
仓储层（Repository）  ◄── 审计日志落库
  ├── memory    ：内存适配器（演示 / 单测，零外部依赖）
  └── postgres  ：PostgreSQL 适配器（JSONB 快照 + 幂等迁移）
```

> Python 只做「鉴权 + 透传 + Agent 编排」，**不复制任何算法**；TypeScript 引擎降级为
> 内网内部服务，是实现 DAG / AOE / 风险 / 燃尽的唯一事实来源。详见 §十。

- **API 层**不复制任何算法，仅做「会话编排 + 入参校验 + 异常映射 + 事件推送 + 审计落库」。
- **多计划隔离**：每个计划一个独立 `PlannerService` 实例，由 `PlanSessionManager` 懒加载并在首次写操作时从仓储恢复。
- **实时性**：任一客户端的写操作成功后，经事件总线通过 WebSocket 广播，其他页面自动刷新。

### 10.2 本地开发（前后端分离）

```bash
# 终端 1：启动 API 服务（默认 http://localhost:8080）
cp .env.example .env            # 首次可执行；Windows: Copy-Item .env.example .env
npm run api:start               # 或 npm run api:dev（tsx watch 热重载）

# 终端 2：启动前端开发服务器（默认 http://localhost:5173，代理 /api 与 /ws）
npm run web:dev
```

打开 `http://localhost:5173` 即进入控制台。开发态由 Vite 代理转发到 API，无需处理跨域。

### 10.3 生产构建（同域一体化托管）

API 服务会自动托管 `WEB_STATIC_DIR` 指定的前端产物，并支持 history 路由回落 `index.html`：

```bash
npm run web:build                                        # 产物输出到 web/dist
WEB_STATIC_DIR=web/dist npm run api:start                # 访问 http://localhost:8080
```

### 10.4 Docker Compose 一键部署（app + PostgreSQL）

```bash
# 1) 准备环境变量（至少设置 API_TOKEN，生产务必开启鉴权）
Copy-Item .env.example .env      # 按需修改 DATABASE_URL 之外的项

# 2) 构建并启动
docker compose up -d --build

# 3) 访问控制台
#    控制台 / API： http://localhost:8080
#    Swagger 文档： http://localhost:8080/api/docs
#    健康检查：     http://localhost:8080/api/health
```

编排包含两个服务：`postgres`（持久化卷 `pgdata`，含 `pg_isready` 健康检查）与 `app`（依赖数据库就绪后启动）。
多层构建仅拷贝依赖清单以命中缓存；运行时以源码直跑（tsx），保证镜像内即是仓库同一份代码。

> 需要 TLS 终止或统一网关时，可选用 [`deploy/nginx.conf`](./deploy/nginx.conf) 作为反代示例（含 WebSocket 升级与长超时配置）。

### 10.5 关键环境变量

完整清单见 [`.env.example`](./.env.example)。核心项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `API_HOST` / `API_PORT` | `0.0.0.0` / `8080` | 监听地址与端口 |
| `API_BASE_PATH` | `/api` | REST 路由前缀（Swagger UI 位于 `<前缀>/docs`） |
| `API_TOKEN` | 空 | Bearer 令牌；**留空表示不鉴权**（健康检查、文档、静态资源始终放行） |
| `API_CORS_ORIGIN` | `*` | 允许来源，多个以英文逗号分隔 |
| `STORAGE_DRIVER` | `memory` | `memory`（演示）或 `postgres`（生产） |
| `DATABASE_URL` | — | PostgreSQL 连接串（优先于离散 `PG*` 参数） |
| `WEB_STATIC_DIR` | 空 | 前端产物目录；存在则同域托管 SPA |
| `PLANNER_FALLBACK_TO_RULE` | `true` | LLM 输出异常时是否降级规则实现 |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | 空 | OpenAI 兼容网关；三项齐全才启用 LLM 主路径 |

### 10.6 开放 API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/health` | 健康检查（版本 / 存储类型 / 运行时长 / WS 订阅数） |
| `GET` | `/api/meta/enums` | 枚举元数据 + 缓冲规则（前端下拉的唯一来源） |
| `POST` | `/api/plans` | 新建计划（完整规划流水线） |
| `GET` | `/api/plans` | 计划摘要列表 |
| `GET` / `DELETE` | `/api/plans/:planId` | 计划详情 / 删除 |
| `POST` | `/api/plans/:planId/replan` | 全量重规划（可覆盖截止时间、开发人数） |
| `GET` / `POST` | `/api/plans/:planId/tasks` | 任务列表 / 新增任务 |
| `PATCH` / `DELETE` | `/api/plans/:planId/tasks/:taskId` | 单字段更新（工时/依赖/状态/优先级/责任人）/ 删除 |
| `GET` | `/api/plans/:planId/graph` | DAG 视图（节点含 critical/isolated/inCycle 标记） |
| `GET` | `/api/plans/:planId/{critical-path,risks,burndown,report,export}` | 关键路径 / 风险 / 燃尽 / 报表 / 导出 |
| `GET` / `POST` | `/api/audit`、`/api/plans/:planId/dag-check` | 审计查询 / 手动校验 DAG |
| `WS` | `/ws` | 计划创建/更新/删除事件推送（含 30s 心跳） |

成功响应统一为 `{ "data": ... }`；错误统一为 `{ "code", "message", "details" }`，并映射到稳定错误码：
`CYCLE_DEPENDENCY`(409)、`VALIDATION_ERROR`(400)、`LLM_FORMAT_ERROR`(422)、`STORAGE_UNAVAILABLE`(503)、`UNAUTHORIZED`(401)。

### 10.7 控制台页面功能

| 页面 | 能力 |
|---|---|
| 计划列表 | 全部计划摘要（任务数 / 关键路径工期 / 高风险计数）、总览·任务·DAG 跳转、删除确认 |
| 新建计划 | 需求录入（含样例快捷填入）、迭代窗口与开发人数、流水线说明与缓冲规则展示 |
| 计划总览 | KPI 卡片、关键路径时间线（es/ls 与进度条）、风险面板（可定位关联任务）、燃尽图、Markdown 报表预览、导出 JSON/MD、重规划弹窗 |
| 任务管理 | 关键字 / 状态 / 关键路径过滤，新增与编辑（保存即重规划）、删除，循环依赖阻断时给出专项提示并定位 |
| 依赖 DAG | AntV X6 画布，按拓扑层级自动布局；关键路径红色、环内橙色虚线、孤立灰底；环 / 悬空依赖顶部告警；缩放与平移；节点详情面板 |
| 审计日志 | 按计划过滤、条数上限、成功/失败与耗时统计、展开查看入参与返回值完整 JSON |

> 前端令牌保存在浏览器 `localStorage`，与后端 `API_TOKEN` 一致即可；未开启鉴权时留空。
> 计划切换、页面刷新、WebSocket 断线重连（5s 自动重试）均已在布局层统一处理。

---

## 十、Python 对外服务与 Agent 编排

> 交付形态：**对外用 Python，内部算法仍是 TypeScript**。Python 侧（[`python/`](./python)）承载对外 API 与 LLM Agent 编排，
> 通过 HTTP 复用已验证的 TS 算法 / 服务（48 项测试全通过），保证算法**单一事实来源**，避免两份实现漂移。

### 10.1 分层与职责

| 层 | 载体 | 职责 |
|---|---|---|
| 对外网关（Python） | [`python/app/routes/proxy.py`](./python/app/routes/proxy.py) | `/api/**` 原样透传（信封 / 状态码 / 导出文件），统一 Bearer 鉴权、CORS、SPA 托管 |
| Agent 编排（Python） | [`python/app/agent/`](./python/app/agent) | 系统提示词 + function calling 循环；每个工具通过 HTTP 调用 TS 引擎 |
| 内部算法引擎（TS） | [`src/api/`](./src/api)、[`src/service/`](./src/service)、[`src/algorithm/`](./src/algorithm) | Kahn 拓扑 / AOE 关键路径 / 缓冲 / 风险 / 燃尽 / 报表，**算法零改动** |
| 控制台（Vue3） | [`web/`](./web) | 仅把 base URL 指向 Python 服务，**页面代码零改动** |

### 10.2 本地启动

```bash
# 终端 1：内部算法引擎（TS，默认 http://localhost:8080）
npm run api:start

# 终端 2：Python 对外服务（默认 http://localhost:8000）
python -m venv python/.venv
python -m pip install -r python/requirements-dev.txt
ENGINE_BASE_URL=http://127.0.0.1:8080 API_PORT=8000 python python/run.py
```

> Windows PowerShell：`$env:ENGINE_BASE_URL="http://127.0.0.1:8080"; $env:API_PORT="8000"; python python/run.py`
> 便捷脚本：`npm run py:install` / `npm run py:start` / `npm run py:test`。

启动后：
- 控制台 / 透传 API：`http://localhost:8000`
- Python Swagger：`http://localhost:8000/docs`
- 健康检查：`http://localhost:8000/api/health`（含引擎可达性）、`/healthz`（Python 自身，容器探活用）

### 10.3 Agent 原生端点（`/api/agent/*`）

需配置 `LLM_BASE_URL / LLM_API_KEY / LLM_MODEL`（OpenAI 兼容网关）后启用；未配置时明确返回 `503 LLM_UNAVAILABLE`。

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/agent/plan` | 自然语言需求 → 完整迭代计划（一次端到端编排） |
| `POST` | `/api/agent/chat` | 会话式对话（携带上下文，多轮修改计划） |
| `GET` | `/api/agent/tools` | Agent 可调用工具清单（OpenAI function calling 格式） |
| `DELETE` | `/api/agent/sessions/:id` | 清空指定会话上下文 |

```jsonc
// POST /api/agent/plan
{ "requirement": "为电商中台开发订单中心 v2.1…", "iterationDeadline": "2026-10-30", "devMemberCount": 3 }
// → { "data": { "content": "…", "planId": "…",
//              "toolCalls": [ { "name": "plan_iteration", "ok": true, "result": {…} } ], "steps": 2 } }
```

### 10.4 Agent 工具 → 内部引擎 REST 映射

Agent 的工具**不实现算法**，仅把 function call 翻译为引擎 REST 调用：

| 工具 | 引擎 REST |
|---|---|
| `plan_iteration` | `POST /api/plans` |
| `replan_all` | `POST /api/plans/:planId/replan` |
| `task_add` / `task_update` / `task_delete` | `POST /api/plans/:planId/tasks`、`PATCH`/`DELETE /api/plans/:planId/tasks/:taskId` |
| `dag_build` / `dag_check` / `schedule_calc` | `GET …/graph`、`POST …/dag-check`、`GET …/critical-path` |
| `risk_scan` / `report_generate` | `GET …/risks`、`GET …/report` |
| `get_plan` / `list_plans` | `GET /api/plans/:planId`、`GET /api/plans` |

### 10.5 错误契约（Python 与引擎完全一致）

成功 `{"data": …}`；失败 `{"code","message","details"}`。稳定错误码：
`CYCLE_DEPENDENCY`(409)、`VALIDATION_ERROR`(400)、`LLM_FORMAT_ERROR`(422)、`STORAGE_UNAVAILABLE`(503)、`UNAUTHORIZED`(401)；
Python 新增：`ENGINE_UNAVAILABLE`(502，引擎不可达)、`ENGINE_CIRCUIT_OPEN`(503，引擎断路器开路)、
`LLM_UNAVAILABLE`(503，未配置网关)、`AGENT_MAX_STEPS`(422，编排未收敛)、
`RATE_LIMITED`(429，触发限流)、`SERVICE_UNAVAILABLE`(503，未就绪/关机)。

### 10.6 测试

```bash
npm run py:test          # 等价于 python -m pytest -c python/pyproject.toml
```

50 个用例（Mock 引擎 + 脚本化 LLM，全程无外部依赖）：信封透传与错误码保持、鉴权、静态 SPA 回落、
Agent 工具调度、工具失败容忍、会话上下文、步数上限，以及企业级加固能力（限流 / 安全头 / 关联 ID /
指标 / 就绪探针 / 断路器 / 重试 / 并行工具调用 / 上下文裁剪 / 会话存储 / LLM 调用重试 / WS 断线重放 /
工具异常兜底 / 输入边界包裹）。

### 10.7 Docker Compose（Python 对外，引擎内网）

`docker compose up -d --build` 编排三个服务：`postgres` + `engine`（TS，仅 `expose` 内网）+ `python`（对外 `APP_PORT`，默认 8080）。
浏览器与外部系统只访问 Python 服务；Python 通过 `ENGINE_BASE_URL=http://engine:8080` 调用引擎。
镜像构建见 [`Dockerfile.python`](./Dockerfile.python)（多阶段：前端构建 → Python 运行时）。

### 10.8 企业级能力（生产加固）

| 能力 | 实现 | 说明 |
|---|---|---|
| 鉴权 | [`security.py`](./python/app/security.py) | Bearer 令牌**常量时间比较**（防时序侧信道） |
| 限流 | [`security.py`](./python/app/security.py) | 按客户端 IP 令牌桶，超限 `429 RATE_LIMITED` + `Retry-After`（`API_RATE_LIMIT_PER_MINUTE`，默认关闭）。**单进程内存实现**：`API_WORKERS>1` 时每个 worker 独立计数，实际全局配额 ≈ 配置值 × worker 数；多副本生产环境建议在网关（Nginx/Ingress）做统一限流，或改用 Redis 全局限流 |
| 安全响应头 | [`security.py`](./python/app/security.py) | `X-Content-Type-Options` / `X-Frame-Options` / `Referrer-Policy` 等，含错误与限流响应 |
| 注入消毒 | [`agent/sanitize.py`](./python/app/agent/sanitize.py) | 剥离用户输入中的伪角色前缀与 ChatML 特殊 token；送入模型前用 `<<<USER_INPUT ... USER_INPUT>>>` 显式边界包裹，让模型区分「指令」与「数据」 |
| 结构化日志 | [`logging_config.py`](./python/app/logging_config.py) | 单行 JSON 日志，自动携带 `requestId` |
| 全链路关联 | [`main.py`](./python/app/main.py) | 生成/透传 `X-Request-Id`，同步下发给引擎与前端 |
| 指标 | [`metrics.py`](./python/app/metrics.py)、`GET /metrics` | Prometheus 文本（请求数/耗时直方图/引擎/Agent/WS），纯标准库零依赖；路径标签按路由模板降基数 |
| 探针 | `GET /healthz`（存活）/ `GET /readyz`（就绪） | 就绪=引擎可达且未在关机；LLM 为可选能力不作门槛 |
| 引擎韧性 | [`resilience.py`](./python/app/resilience.py)、[`engine_client.py`](./python/app/engine_client.py) | 连接/读取分离超时、连接池上限、指数退避重试（仅连接失败与幂等方法的 5xx）、三态断路器 |
| 优雅关机 | [`main.py`](./python/app/main.py) | 关机时置 `shutting_down` → `/readyz` 返回 503，随后停止桥接并关闭连接池 |
| Agent 增强 | [`agent/runner.py`](./python/app/agent/runner.py)、[`agent/llm.py`](./python/app/agent/llm.py) | 同一消息内**并行**执行工具调用（保序回填）、上下文预算超限自动裁剪最旧历史、工具入参结构校验、LLM 调用重试 |
| 会话存储 | [`sessions.py`](./python/app/sessions.py) | 可插拔：内存（LRU + TTL + 后台清理）或 Redis（多副本共享）；同会话串行化（会话级锁） |
| 多进程 | `run.py`、`API_WORKERS` | 多 worker 需 `SESSION_STORE=redis` + 网关层 WS 粘性会话；限流与断路器均为**进程内**状态，多 worker/多副本下应上移到网关或改用 Redis 全局实现 |

运维端点：`http://<host>/healthz`、`/readyz`、`/metrics`（均免鉴权，便于 k8s 探针与 Prometheus 抓取）。

---

## 十一、已知边界

- **算法口径修正**：§5.2 表格中两处已与需求方确认，其余严格遵循需求文档。
- **LLM 拆解质量**依赖所接入模型；未接入 LLM 时使用规则拆解（按需求关键词映射任务模板），保证离线可运行。
- **扩展接口**（Jira / Git / CI-CD）当前仅为接口占位，尚未实现具体适配器。
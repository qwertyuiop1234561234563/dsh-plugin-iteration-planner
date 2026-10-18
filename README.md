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
├── .github/
│   └── workflows/
│       └── ci.yml               # 【CI 门禁】引擎单测 / Python 单测（含真实 Redis 服务）/ 控制台类型检查
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
│   │   ├── agent/               # 【Agent 编排层】提示词 / 工具定义与调度 / LLM 客户端 / function calling 循环 / 长期记忆
│   │   ├── kb/                  # 【知识库 RAG】向量化 / Chroma 向量库 / 递归分块 / 文档解析 / 混合检索（BM25 + RRF）
│   │   └── routes/              # 系统路由 / Agent / 知识库 / 长期记忆 / 治理工作区 / 网关透传（SPA 托管）
│   ├── tests/                   # pytest（Mock 引擎 + 脚本化 LLM + 桩向量化，覆盖转发/错误/鉴权/Agent/知识库/记忆）
│   ├── requirements.txt         # 运行时依赖
│   ├── requirements-dev.txt     # 开发测试依赖
│   ├── requirements-redis.txt   # 可选依赖：Redis 共享后端（会话 / 限流 / 缓存）
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
│   │   ├── views/               # 页面（计划 / 治理 / 分析 / 知识库 / 长期记忆 / 审计）
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

### 9.1 架构与数据流

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

### 9.2 本地开发（前后端分离）

```bash
# 终端 1：启动 API 服务（默认 http://localhost:8080）
cp .env.example .env            # 首次可执行；Windows: Copy-Item .env.example .env
npm run api:start               # 或 npm run api:dev（tsx watch 热重载）

# 终端 2：启动前端开发服务器（默认 http://localhost:5173，代理 /api 与 /ws）
npm run web:dev
```

打开 `http://localhost:5173` 即进入控制台。开发态由 Vite 代理转发到 API，无需处理跨域。

> **上面只起了「引擎 + 前端」两件套**（不含 Agent 能力，`/api/agent/*` 会返回 404）：
> AI 助手、知识库与长期记忆等 Agent 能力都由 **Python 对外服务** 提供，需另按 §10.2 启动
> （`ENGINE_BASE_URL=http://127.0.0.1:8080 GATEWAY_PORT=8000 python python/run.py`，前端代理指向它）。
> 生产一体化托管（含 Agent）见 §10.7。

### 9.3 生产构建（同域一体化托管）

API 服务会自动托管 `WEB_STATIC_DIR` 指定的前端产物，并支持 history 路由回落 `index.html`：

```bash
npm run web:build                                        # 产物输出到 web/dist
WEB_STATIC_DIR=web/dist npm run api:start                # 访问 http://localhost:8080
```

### 9.4 Docker Compose 一键部署（app + PostgreSQL）

```bash
# 1) 准备环境变量（账号体系默认开启；引导管理员口令留空会自动生成并打印一次）
Copy-Item .env.example .env      # 按需修改 DATABASE_URL 之外的项

# 2) 构建并启动
docker compose up -d --build

# 3) 访问控制台（首次启动日志里会打印引导管理员账号与随机口令）
#    控制台 / API： http://localhost:8080
#    Swagger 文档： http://localhost:8080/api/docs
#    健康检查：     http://localhost:8080/api/health
```

编排包含两个服务：`postgres`（持久化卷 `pgdata`，含 `pg_isready` 健康检查）与 `app`（依赖数据库就绪后启动）。
多层构建仅拷贝依赖清单以命中缓存；运行时以源码直跑（tsx），保证镜像内即是仓库同一份代码。

> 需要 TLS 终止或统一网关时，可选用 [`deploy/nginx.conf`](./deploy/nginx.conf) 作为反代示例（含 WebSocket 升级与长超时配置）。

### 9.5 关键环境变量

完整清单见 [`.env.example`](./.env.example)。核心项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `API_HOST` / `API_PORT` | `0.0.0.0` / `8080` | **TS 算法引擎**监听地址与端口（引擎与网关共用仓库根同一份 `.env`，故端口变量必须区分） |
| `GATEWAY_PORT` | `8080` | **Python 网关**监听端口（本地联调推荐 `8000`，见 §10.2）。刻意不复用 `API_PORT`：同名会让引擎与网关抢同一个端口 |
| `API_BASE_PATH` | `/api` | REST 路由前缀（Swagger UI 位于 `<前缀>/docs`） |
| `API_TOKEN` | 空 | **机器 / CI 令牌**（服务账号语义，等价管理员但无人类身份）；**留空表示不启用机器令牌**（健康检查、文档、静态资源始终放行）。人类用户一律走登录 + 会话 Cookie，见 §10.10 |
| `AUTH_ENABLED` | `true` | 登录 + 角色鉴权总开关；关闭后 `/api/**` 与 `/ws` 整体不鉴权（仅用于本地排障） |
| `AUTH_STORE_PATH` | 空 | 账号 / 会话库（sqlite3）路径；留空用 `python/.data/auth.db`（已 gitignore），`:memory:` 为进程内存库（重启即丢，仅演示 / 测试） |
| `AUTH_SESSION_COOKIE` / `AUTH_SESSION_TTL_SECONDS` | `ip_session` / `28800` | 会话 Cookie 名 / 有效期（秒）；过期自动失效，管理员可即时吊销 |
| `AUTH_COOKIE_SECURE` / `AUTH_COOKIE_SAMESITE` | `false` / `lax` | Cookie 是否仅经 HTTPS 传输（**生产经 HTTPS 务必 `true`**）/ SameSite 策略（填 `none` 时浏览器要求 `secure=true`） |
| `AUTH_BOOTSTRAP_ADMIN_USERNAME` / `AUTH_BOOTSTRAP_ADMIN_PASSWORD` | `admin` / 空 | 首启引导管理员；账号库非空时**忽略**（不会重置既有口令）。口令留空则自动生成随机口令并以 WARNING 日志打印**一次**，登录后请立即更换 |
| `API_CORS_ORIGIN` | `*` | 允许来源，多个以英文逗号分隔 |
| `STORAGE_DRIVER` | `memory` | `memory`（演示）或 `postgres`（生产） |
| `DATABASE_URL` | — | PostgreSQL 连接串（优先于离散 `PG*` 参数） |
| `WEB_STATIC_DIR` | 空 | 前端产物目录；存在则同域托管 SPA |
| `PLANNER_FALLBACK_TO_RULE` | `true` | LLM 输出异常时是否降级规则实现 |
| `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` | 空 / 空 / 空 | OpenAI 兼容网关；**配置端点与模型即启用** LLM 主路径（密钥可选，兼容无鉴权的自建网关 vLLM / Ollama）。智谱 GLM 示例：`https://open.bigmodel.cn/api/paas/v4` + `glm-4.7-flash`（**文本模型长期免费**，支持流式输出与 Function Calling） |
| `EMBEDDING_BASE_URL` / `EMBEDDING_MODEL` / `EMBEDDING_API_KEY` | `https://api.siliconflow.cn/v1` / `BAAI/bge-m3` / 空 | 向量化网关；默认硅基流动 `BAAI/bge-m3`（**长期免费**，固定 1024 维）。**端点与密钥绑定**：`EMBEDDING_BASE_URL` 非空时只认 `EMBEDDING_API_KEY`，不会拿 `LLM_API_KEY` 去请求别家（避免 401 污染指标）；两行都留空才回落 `LLM_BASE_URL` / `LLM_API_KEY`（如智谱 `embedding-3`，按量计费） |
| `EMBEDDING_DIMENSIONS` / `EMBEDDING_SEND_DIMENSIONS` / `EMBEDDING_BATCH_SIZE` | `1024` / `false` / `10` | 期望向量维度（校验与缓存键）/ 是否下发 `dimensions` 字段（仅支持自定义维度的模型如智谱 `embedding-3`、`Qwen/Qwen3-Embedding` 需开启；`bge-m3` 等固定维度模型下发会被上游判为非法参数）/ 单次请求条数（上游普遍上限 64） |
| `VECTOR_STORE` / `CHROMA_DIR` | `chroma` / 空 | 向量库驱动 / 持久化目录（留空用 `python/.data/chroma`，已 gitignore） |
| `KB_CHUNK_SIZE` / `KB_CHUNK_OVERLAP` / `KB_TOP_K` | `800` / `120` / `4` | 分块字符数 / 重叠字符数 / 默认召回条数 |
| `KB_HYBRID` / `KB_RRF_K` / `KB_AUTO_INJECT` / `KB_INJECT_TOP_K` | `true` / `60` / `true` / `3` | 混合检索开关 / RRF 平滑常数 / 对话自动注入开关 / 注入条数 |
| `KB_MAX_DOCUMENT_BYTES` / `KB_MAX_DOCUMENTS` | `20971520` / `500` | 单文档体积上限 / 文档总数上限 |
| `MEMORY_ENABLED` / `MEMORY_EXTRACT` / `MEMORY_TOP_K` | `true` / `true` / `4` | 记忆总开关 / 自动提取开关 / 每轮注入条数 |
| `MEMORY_DEDUP_THRESHOLD` / `MEMORY_MAX_ITEMS` | `0.92` / `2000` | 去重相似度阈值 / 记忆总量上限 |
| `API_MAX_BODY_BYTES` | `33554432` | 请求体大小上限（字节，`0` 关闭）；超限即 413 `PAYLOAD_TOO_LARGE`，须 ≥ `KB_MAX_DOCUMENT_BYTES` + multipart 开销 |
| `KB_DETAIL_PREVIEW_CHUNKS` | `50` | 文档详情返回的分块**预览**条数上限（`chunkCount` 始终为真实总数）；超大文档分块上万，全量返回会让单次响应达数十 MB |
| `WS_SEND_TIMEOUT_SECONDS` / `WS_MAX_CLIENTS` | `5.0` / `512` | 单客户端单条事件下发超时（秒，超时即断开该连接）/ 在线客户端上限（`0` 不限，超出以 close code `1013` 拒绝）；扇出为并发下发，慢客户端不会拖住其余客户端 |
| `AGENT_COLLAB_MAX_STEPS` | `6` | 多角色协作（需求分析 / 引擎排期 / 估算审阅 / 风险缓解 / 质量校验）单次最多产出的角色步骤数 |
| `PATROL_ENABLED` / `PATROL_INTERVAL_SECONDS` | `true` / `300` | 主动巡检开关 / 巡检周期（秒）；首轮**延迟一个周期**执行，不在启动瞬间集中打引擎 |
| `PATROL_MAX_PLANS` / `PATROL_ALERT_COOLDOWN_SECONDS` | `20` / `1800` | 单轮最多巡检计划数（截断保护引擎）/ 同一信号重复推送的冷却时间（秒，`0` 表示不去重） |
| `PATROL_FINDING_TTL_SECONDS` / `PATROL_MAX_FINDINGS` | `86400` / `200` | 巡检发现保留时长（秒）/ 条数上限（限制长期运行的内存增长） |
| `PATROL_ESCALATE_SEVERITY` | `critical` | 达到该严重度即视为「需立即处理」并升级通知：`info` \| `warning` \| `critical` |
| `RETRO_WRITE_MEMORY` / `RETRO_MAX_MEMORIES` | `true` / `4` | 复盘结论是否写回长期记忆（闭环学习）/ 单次最多写回条数 |
| `WHATIF_MAX_TASK_EDITS` | `10` | What-if 沙盘单次最多接受的任务级扰动条数（防止临时计划失去可比性） |

### 9.6 开放 API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/health` | 健康检查（版本 / 存储类型 / 运行时长 / WS 订阅数） |
| `GET` | `/api/meta/enums` | 枚举元数据 + 缓冲规则（前端下拉的唯一来源） |
| `POST` | `/api/plans` | 新建计划（完整规划流水线）。**带 `Prefer: respond-async` 时改为异步作业**：立即返回 `202` + 作业句柄，进度经 SSE 下发（详见 §10.12） |
| `GET` | `/api/plans` | 计划摘要列表 |
| `GET` / `DELETE` | `/api/plans/:planId` | 计划详情 / 删除 |
| `POST` | `/api/plans/:planId/replan` | 全量重规划（可覆盖截止时间、开发人数）。同样支持 `Prefer: respond-async` 异步作业 |
| `GET` / `POST` | `/api/plans/:planId/tasks` | 任务列表 / 新增任务 |
| `PATCH` / `DELETE` | `/api/plans/:planId/tasks/:taskId` | 单字段更新（工时/依赖/状态/优先级/责任人）/ 删除 |
| `GET` | `/api/plans/:planId/graph` | DAG 视图（节点含 critical/isolated/inCycle 标记） |
| `GET` | `/api/plans/:planId/{critical-path,risks,burndown,report,export}` | 关键路径 / 风险 / 燃尽 / 报表 / 导出 |
| `GET` / `POST` | `/api/audit`、`/api/plans/:planId/dag-check` | 审计查询 / 手动校验 DAG |
| `*` | `/api/kb/**`、`/api/memories/**` | 知识库（RAG）与长期记忆（Python 服务提供，详见 §10.9） |
| `*` | `/api/auth/**` | 登录 / 注销 / 会话查询 / 改口令 / 账号管理 / 计划指派（Python 服务提供，详见 §10.10） |
| `GET` | `/api/jobs/:jobId` | 异步写作业快照（状态 / 进度 / 结果）；**仅属主可读，否则 404**（详见 §10.12） |
| `GET` | `/api/jobs/:jobId/stream` | 作业进度 SSE 流（`progress` → `done`/`error`，15s 心跳） |
| `POST` | `/api/jobs/:jobId/cancel` | 取消作业（置终态，不保证中断引擎侧同步计算） |
| `WS` | `/ws` | 计划创建/更新/删除事件推送（含 30s 心跳）。**握手需鉴权**：浏览器自动携带会话 Cookie（同源）；机器 / CI 用 `?token=<API_TOKEN>`（`WebSocket` 构造函数无法自定义请求头）。未通过即在握手阶段被拒（close code `1008`，HTTP 403），不下发任何事件。**访客（guest）只收到被指派计划的事件**，其余事件在扇出前即被过滤 |

成功响应统一为 `{ "data": ... }`；错误统一为 `{ "code", "message", "details" }`，并映射到稳定错误码：
`CYCLE_DEPENDENCY`(409)、`VALIDATION_ERROR`(400)、`LLM_FORMAT_ERROR`(422)、`STORAGE_UNAVAILABLE`(503)、`UNAUTHORIZED`(401)、`FORBIDDEN`(403)、
`ENGINE_TIMEOUT`(504，可达但超时，带 `Proxy-Status: iteration_planner; error="http_response_timeout"`)、`ENGINE_UNAVAILABLE`(502，连不上)、
`JOB_NOT_FOUND`(404)、`JOB_CONFLICT`(409)、`JOB_STORE_UNAVAILABLE`(503)、`IDEMPOTENCY_IN_PROGRESS`(409，带 `Retry-After`)、`IDEMPOTENCY_KEY_REUSE`(422)、`PAYLOAD_TOO_LARGE`(413)。

### 9.7 控制台页面功能

| 页面 | 能力 |
|---|---|
| 计划列表 | 全部计划摘要（任务数 / 关键路径工期 / 高风险计数）、总览·任务·DAG 跳转、删除确认 |
| 新建计划 | 需求录入（含样例快捷填入）、迭代窗口与开发人数、流水线说明与缓冲规则展示 |
| 计划总览 | KPI 卡片、关键路径时间线（es/ls 与进度条）、风险面板（可定位关联任务）、燃尽图、Markdown 报表预览、导出 JSON/MD、重规划弹窗 |
| 任务管理 | 关键字 / 状态 / 关键路径过滤，新增与编辑（保存即重规划）、删除，循环依赖阻断时给出专项提示并定位 |
| 依赖 DAG | AntV X6 画布，按拓扑层级自动布局；关键路径红色、环内橙色虚线、孤立灰底；环 / 悬空依赖顶部告警；缩放与平移；节点详情面板 |
| 知识库 | 文档清单（格式 / 分块数 / 字符数 / 标签）、上传文件或粘贴文本入库、分块预览、删除；检索试跑展示命中片段与得分，并可切换混合 / 纯向量模式 |
| 长期记忆 | 记忆清单（类型 / 重要度 / 状态 / 来源 / 最近使用）、按状态与类型过滤、关键词搜索、启用停用、删除与清空、手工新增（写入前自动去重）；语义召回试跑展示相似度排序 |
| AI 助手 | 全局悬浮球 + 会话抽屉（SSE 逐字流式、工具调用明细、知识库引用与召回记忆卡片、写操作两阶段确认）；顶部可在**对话 / 多角色协作 / What-if 沙盘 / 迭代复盘 / 主动巡检**之间切换，后四者见 §10.3.1 |
| 审计日志 | 按计划过滤、条数上限、成功/失败与耗时统计、展开查看入参与返回值完整 JSON |
| 账号与权限 | **仅管理员可见**：账号清单（角色 / 状态 / 最近登录）、新建账号、变更角色与状态、重置口令、删除；为访客（guest）指派可见计划白名单 |
| 登录页 | 账号口令登录；登录态为服务端会话 Cookie，成功后跳回被守卫拦下的目标页 |

> **登录态不在浏览器本地存凭据**：服务端下发 `httpOnly` 会话 Cookie（脚本不可读，防 XSS 窃取），
> 页面刷新经 `GET /api/auth/session` 复原；菜单与入口按角色权限收敛，但**权限判定一律在服务端**完成。
> WebSocket 因浏览器 API 无法自定义请求头，改为由浏览器自动携带同源 Cookie（见 §10.10）。
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
| 知识库 RAG（Python） | [`python/app/kb/`](./python/app/kb) | 文档解析 → 递归分块 → 批量向量化（默认硅基流动 `BAAI/bge-m3`）→ Chroma 持久化；检索走稠密向量 + BM25 → RRF 融合 |
| 长期记忆（Python） | [`python/app/agent/memory.py`](./python/app/agent/memory.py) | 每轮对话后自动提取（Mem0 风格）→ 向量去重 → 语义召回 top-k 注入上下文 |
| 内部算法引擎（TS） | [`src/api/`](./src/api)、[`src/service/`](./src/service)、[`src/algorithm/`](./src/algorithm) | Kahn 拓扑 / AOE 关键路径 / 缓冲 / 风险 / 燃尽 / 报表，**算法零改动** |
| 控制台（Vue3） | [`web/`](./web) | 仅把 base URL 指向 Python 服务，**页面代码零改动** |

### 10.2 本地启动

```bash
# 终端 1：内部算法引擎（TS，默认 http://localhost:8080）
npm run api:start

# 终端 2：Python 对外服务（默认 http://localhost:8000；端口变量为 GATEWAY_PORT，不是 API_PORT）
python -m venv python/.venv
python -m pip install -r python/requirements-dev.txt
ENGINE_BASE_URL=http://127.0.0.1:8080 GATEWAY_PORT=8000 python python/run.py
```

> Windows PowerShell：`$env:ENGINE_BASE_URL="http://127.0.0.1:8080"; $env:GATEWAY_PORT="8000"; python python/run.py`
> **端口变量必须区分**：`API_PORT` 属于 TS 引擎，`GATEWAY_PORT` 属于 Python 网关。二者共用仓库根同一份 `.env`，
> 若统一用 `API_PORT`，两个进程会去绑定同一个端口（先起者占用、后起者启动失败）。
> 便捷脚本：`npm run py:install` / `npm run py:start` / `npm run py:test`。
> 启用知识库与长期记忆：在仓库根 `.env` 中配置 `EMBEDDING_API_KEY`（留空则回落 `LLM_API_KEY`）；
> 未配置时二者自动降级为「不可用」，主链路（算法与对话）不受影响。**注意 `Settings` 按进程当前目录读取 `.env`，需从仓库根启动。**

启动后：
- 控制台 / 透传 API：`http://localhost:8000`
- Python Swagger：`http://localhost:8000/docs`
- 健康检查：`http://localhost:8000/api/health`（含引擎可达性）、`/healthz`（Python 自身，容器探活用）

### 10.3 Agent 原生端点（`/api/agent/*`）

需配置 `LLM_BASE_URL / LLM_MODEL`（OpenAI 兼容网关）后启用；未配置时明确返回 `503 LLM_UNAVAILABLE`（前端自动回落内置规则问答）。

以智谱 GLM（BigModel 开放平台，**文本模型长期免费**）为例：把配置写入仓库根目录 `.env`（已被 `.gitignore` 忽略，不会入库），重启服务即生效。

```ini
# 对话与 Agent 编排：glm-4.7-flash 为长期免费文本模型（支持流式输出 + Function Calling）
LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
LLM_API_KEY=xxxxxxxxxxxxxxxx.xxxxxxxxxxxxxxxx
LLM_MODEL=glm-4.7-flash

# 知识库向量化（可选）：推荐硅基流动 BAAI/bge-m3（长期免费，1024 维）
EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1
EMBEDDING_API_KEY=sk-xxxxxxxxxxxxxxxx
EMBEDDING_MODEL=BAAI/bge-m3
```

`glm-4.7-flash` / `glm-4.5-flash` / `glm-4-flash` 为智谱**长期免费**文本模型（不消耗额度，支持流式输出与 Function Calling）；
向量化默认的硅基流动 `BAAI/bge-m3` 同样**长期免费**。若希望向量化与 LLM 共用同一端点（智谱 `embedding-3`，按量计费
0.5 元/百万 Tokens，支持 256–2048 维自定义、单次最多 64 条），把 `EMBEDDING_BASE_URL` 与 `EMBEDDING_API_KEY`
留空以回落 LLM 配置，并设置 `EMBEDDING_MODEL=embedding-3`、`EMBEDDING_SEND_DIMENSIONS=true`。

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/agent/plan` | 自然语言需求 → 完整迭代计划（一次端到端编排） |
| `POST` | `/api/agent/chat` | 会话式对话（携带上下文，多轮修改计划） |
| `POST` | `/api/agent/chat/stream` | 同上，但以 **SSE 流式输出**（逐字下发 + 工具/待确认事件） |
| `POST` | `/api/agent/collab/stream` | **多角色协作**（SSE 步骤流：需求分析 → 引擎排期 → 估算审阅 → 风险缓解 → 质量校验） |
| `GET` | `/api/agent/collab/steps` | 协作角色与步骤说明（前端时间线图例） |
| `POST` | `/api/agent/whatif` | **What-if 沙盘**：自然语言情景 → 引擎重算 → before/after 增量对比 |
| `POST` | `/api/agent/retro` | **迭代复盘 + 学习**：确定性统计 + 结论（可选写回长期记忆） |
| `GET` | `/api/agent/patrol/findings` | 主动巡检发现清单（含运行状态与信号图例） |
| `POST` | `/api/agent/patrol/run` | 立即执行一轮巡检（只读 + 推送增量预警） |
| `GET` | `/api/agent/patrol/signals` | 巡检信号与严重度说明（前端图例） |
| `GET` | `/api/agent/tools` | Agent 可调用工具清单（OpenAI function calling 格式） |
| `DELETE` | `/api/agent/sessions/:id` | 清空指定会话上下文 |

```jsonc
// POST /api/agent/plan
{ "requirement": "为电商中台开发订单中心 v2.1…", "iterationDeadline": "2026-10-30", "devMemberCount": 3 }
// → { "data": { "content": "…", "planId": "…",
//              "toolCalls": [ { "name": "plan_iteration", "ok": true, "result": {…} } ], "steps": 2 } }
```

`POST /api/agent/chat/stream` 返回 `text/event-stream`，每帧为 `event: <name>\ndata: <json>\n\n`，
事件契约与前端消费方式（`fetch` + `ReadableStream`，支持 `AbortController` 中断）：

| 事件 | 载荷 | 说明 |
|---|---|---|
| `meta` | `{ sessionId, references, memories }` | 首帧，回传会话 ID 与本轮检索增强上下文 |
| `delta` | `{ text }` | 模型增量文本（逐字下发；不可流式的模型回退为单帧全量） |
| `tool_start` | `{ name, arguments }` | 开始调用某个引擎工具 |
| `tool_result` | `{ name, arguments, ok, error? }` | 工具返回摘要（明细在 `final.toolCalls`） |
| `pending` | `{ actions }` | 待用户确认的写操作（两阶段确认，确认前不落库） |
| `step` | `{ role, status, title, detail, at }` | **仅多角色协作**（`/api/agent/collab/stream`）角色时间线，见 §10.3.1 |
| `final` | `AgentChatResponse` | 收尾帧，与非流式 `/api/agent/chat` 响应结构一致 |
| `error` | `{ code, message, details }` | 编排失败，随后关闭连接 |

服务端每 15s 发送一行注释心跳（`: ping`）防止中间代理空闲断连；
调用方主动断开（如前端「停止生成」）时后端会取消进行中的编排并释放连接。

> **反代部署注意**：SSE 会被 Nginx / Ingress 的默认响应缓冲吃掉（表现为「一次性吐出全部内容」）。
> 应用已回 `X-Accel-Buffering: no` 与 `Cache-Control: no-cache, no-transform`，若仍被缓冲，请在该路径上显式关闭：
> `location /api/agent/chat/stream { proxy_buffering off; proxy_cache off; proxy_read_timeout 3600s; }`。

### 10.3.1 进阶 Agent 能力（多角色协作 / What-if 沙盘 / 迭代复盘 / 主动巡检）

四项能力都遵循同一条硬约束：**不重写算法**。所有工期、关键路径、风险均取自引擎返回值，
Python 侧只做「解析 + 编排 + 确定性算术聚合」（[`agent/analytics.py`](./python/app/agent/analytics.py) 为纯函数层，无 IO、无状态）。

**① 多角色协作（plan-act-observe 步骤流）**

`POST /api/agent/collab/stream` 把一句话需求交给多角色跑完整链路，中间步骤**可见、可中断**：

| 角色 | 职责 | 是否建模 |
|---|---|---|
| 需求分析（planner） | 需求 → 任务拆解草案 | 是（未配置 LLM 时用确定性拆解） |
| 排期计算（scheduler） | 调引擎完成 拆解 → DAG → AOE → 风险 → 燃尽，**真实创建计划** | 否（纯引擎调用） |
| 估算审阅（estimator） | 审阅工时与依赖，给出**建议** | 是 |
| 风险缓解（risk） | 复用风险顾问口径给出缓解动作 | 是 |
| 质量校验（critic） | 验收点覆盖 / 关键路径责任人 / 交付日期核对 | 是 + 确定性核对合并 |

事件在对话流（`meta`/`delta`/`tool_start`/`tool_result`/`pending`/`final`/`error`）之上**新增 `step`**：

```jsonc
// event: step  data: { "role": "scheduler", "status": "running", "title": "排期计算（算法引擎）", "detail": "…", "at": 1760000000 }
```

`step.status ∈ pending | running | done | failed | skipped`。估算审阅给出的修正只进 `pending`
（两阶段确认，**不自动落库**）；需要 `agent:write` 权限。

**② What-if 沙盘（自然语言仿真）**

`POST /api/agent/whatif` `{ planId, instruction }`。解析路径优先用 LLM，未配置时降级为正则规则解析
（`mode=rule` + `degradedReason`），支持「延期 N 天」「提前 N 天」「加/减 N 人」「工时改为 N」「责任人改为 X」「状态改为 X」。

实现要点：**先建临时计划 → 逐条 `PATCH` 落地扰动 → 重读快照 → 在 `finally` 中删除临时计划**，
因此仿真**不会污染基线**，返回体带 `ephemeral: true`；`edits[]` 逐条回传是否应用成功及原因，
无法映射为改动时 `unparsed: true`（不编造结论）。

**③ 迭代复盘 + 学习**

`POST /api/agent/retro` `{ planId, actualHours?, materializedRiskIds?, scopeChanges?, writeMemory? }`。

- 统计**确定性可复算**：进度（`doneRatio`/`elapsedRatio`/`driftDays`）、交付偏移、风险兑现率、工时偏差与校准曲线；
- 实际工时与「已兑现风险」是**外部事实**，引擎计划中不存在：未提供时 `effort.available=false` 并给出原因，**绝不编造**；
- `writeMemory=true` 时把校准结论写回长期记忆（`add_memory` 去重，`memoryWritten` 回传实际条数），闭环反哺后续规划；
- 未配置 LLM 时用规则结论（`_rule_conclusions`），同样显式标注 `mode`。

**④ 主动巡检 Agent（后台定时 + 增量预警）**

后台任务按 `PATROL_INTERVAL_SECONDS` 周期巡检可见计划，只做两件事：**聚合引擎事实** + **与上一次快照增量比对**。

| 信号 | 来源（全部为引擎事实） |
|---|---|
| `progress_drift` 进度偏差 | 任务状态 + 燃尽数据（`driftDays` ≥ 1 或存在 DELAYED） |
| `critical_path_drift` 关键路径漂移 | 引擎 `criticalPath` 变化（独立记录上一轮指纹，避免误报） |
| `resource_overload` 人员过载 | 引擎 `riskList` 中的单日工时超限 |
| `dependency_blocked` 依赖阻塞 | 引擎 `riskList` 中的循环依赖 / 孤立任务 |
| `deadline_risk` 交付风险 | 引擎 `riskList` 中的关键路径 DELAYED + 计划完成晚于截止 |

去重与止损：同一信号在 `PATROL_ALERT_COOLDOWN_SECONDS` 冷却期内不重复推送；信号消失时下发
`resolved`（恢复）通知；`PATROL_MAX_FINDINGS` / `PATROL_FINDING_TTL_SECONDS` 限制内存增长；
引擎不可达时返回空摘要并记录错误，**不抛异常、不阻断交互式请求**。发现经 WebSocket
（`type=patrol.finding`）推送，前端由通知中心接住，越权计划不下发（按用户可见范围过滤）。

前端呈现：AI 助手抽屉顶部可在「对话 / 多角色协作 / What-if 沙盘 / 迭代复盘 / 主动巡检」之间切换
（见 [`AgentWorkbench.vue`](./web/src/components/AgentWorkbench.vue)），写类入口按 `agent:write` 收敛。

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
| `kb_search` / `kb_list_documents` | **Python 本地**（Chroma 向量库 + BM25，不经过引擎） |
| `memory_search` | **Python 本地**（长期记忆语义召回，不经过引擎） |

### 10.5 错误契约（Python 与引擎完全一致）

成功 `{"data": …}`；失败 `{"code","message","details"}`。稳定错误码：
`CYCLE_DEPENDENCY`(409)、`VALIDATION_ERROR`(400)、`LLM_FORMAT_ERROR`(422)、`STORAGE_UNAVAILABLE`(503)、`UNAUTHORIZED`(401)；
Python 新增：`ENGINE_UNAVAILABLE`(502，引擎不可达)、`ENGINE_CIRCUIT_OPEN`(503，引擎断路器开路)、
`LLM_UNAVAILABLE`(503，未配置网关)、`AGENT_MAX_STEPS`(422，编排未收敛)、
`RATE_LIMITED`(429，触发限流)、`PAYLOAD_TOO_LARGE`(413，请求体超过 `API_MAX_BODY_BYTES`)、
`SERVICE_UNAVAILABLE`(503，未就绪/关机)、
`EMBEDDING_UNAVAILABLE`(503，未配置向量化模型)、`EMBEDDING_GATEWAY_ERROR`(502，向量化网关异常)、
`VECTOR_STORE_UNAVAILABLE`(503，Chroma 不可用/集合未知)、`SESSION_STORE_UNAVAILABLE`(503，会话存储后端异常)。

### 10.6 测试

```bash
npm run py:test          # 等价于 python -m pytest -c python/pyproject.toml
```

382 个用例（Mock 引擎 + 脚本化 LLM + 桩向量化，全程无外部依赖）：信封透传与错误码保持、鉴权、静态 SPA 回落、
Agent 工具调度、工具失败容忍、会话上下文、步数上限，企业级加固能力（限流 / 请求体上限 / 安全头 / 关联 ID /
指标 / 就绪探针 / 断路器 / 重试 / 并行工具调用 / 上下文裁剪 / 会话存储 / LLM 调用重试 / WS 断线重放 /
工具异常兜底 / 错误响应脱敏 / 输入边界包裹 / 会话 ID 白名单），**访问控制全链路**（scrypt 口令 / 角色权限矩阵 /
账号 CRUD 与最后管理员保护 / 会话创建吊销过期 / 改角色与停用即时踢下线 / 访客计划作用域 404 与列表过滤 /
WS 握手 Cookie 鉴权与广播过滤），以及**知识库全链路**（分块 / 文档解析 /
BM25 / RRF / 向量库增删查 / 入库-检索-删除 REST）与**长期记忆**（候选解析 / 去重 / 语义召回 / 停用治理 /
自动提取 / 后台提取失败消费 / agentic RAG 工具），以及**进阶 Agent 能力**（协作步骤流与无 LLM 降级 /
估算建议两阶段不落库 / 沙盘规则与 LLM 解析路径 + 临时计划回收 + 非法扰动丢弃 / 巡检增量·冷却·恢复·引擎故障不影响 /
复盘规则降级 + 记忆写回与检索校验 + 无实际工时标记不可用）。

### 10.7 Docker Compose（Python 对外，引擎内网）

`docker compose up -d --build` 编排三个服务：`postgres` + `engine`（TS，仅 `expose` 内网）+ `python`（对外 `APP_PORT`，默认 8080）。
浏览器与外部系统只访问 Python 服务；Python 通过 `ENGINE_BASE_URL=http://engine:8080` 调用引擎。
镜像构建见 [`Dockerfile.python`](./Dockerfile.python)（多阶段：前端构建 → Python 运行时）。

> **需要 Redis 时不要只配 `REDIS_URL` 而不起实例**：compose 中的 `redis` 服务挂在 `redis` profile 下
> （默认不启动，避免单副本部署多跑一个容器）。多副本 / 多 worker 共享会话时请一并拉起并成对配置：
>
> ```bash
> docker compose --profile redis up -d --build
> # 环境变量：SESSION_STORE=redis  REDIS_URL=redis://redis:6379/0
> ```
>
> 只配 `SESSION_STORE=redis` 而没有实例时服务会**启动即失败**（刻意的，避免「以为共享了会话其实各存各的」）。

### 10.8 企业级能力（生产加固）

| 能力 | 实现 | 说明 |
|---|---|---|
| 鉴权（人类） | [`auth/`](./python/app/auth) | 登录 + 角色 + 服务端会话：口令用 **scrypt**（内存硬，抵御 GPU 爆破）加盐散列；会话 Cookie 为 `httpOnly` 随机令牌，落库只存其 `sha256` 摘要（库被读走也无法回放伪造），**可即时吊销**（管理员踢下线 / 改口令即失效）；登录失败统一返回同一句文案，避免账号枚举（账号不存在时也做一次等价耗时校验，防时序枚举） |
| 鉴权（机器） | [`security.py`](./python/app/security.py) | `API_TOKEN` Bearer 令牌**常量时间比较**（防时序侧信道），视为内部服务账号；人类用户不再共享此令牌 |
| 授权 | [`auth/permissions.py`](./python/app/auth/permissions.py) | 角色 → 权限、请求「方法 + 路径」→ 所需权限的**单一映射表**；权限不足 `403 FORBIDDEN`（区别于 401：重登无效，须调整角色）。访客越权访问计划按 `404` 处理，不泄露计划存在性 |
| WebSocket 鉴权与作用域 | [`routes/system.py`](./python/app/routes/system.py)、[`ws.py`](./python/app/ws.py) | WS 走独立 ASGI scope，`BaseHTTPMiddleware` 式鉴权**不覆盖**它——必须在端点内显式校验（会话 Cookie 或 `?token=`，常量时间比较），否则任意客户端直连 `/ws` 即可拿到 `plan.created` 全量载荷（需求原文 + 任务明细）。**访客连接只接收被指派计划的事件**，其余事件在扇出前过滤（无 `planId` 的事件同样不下发，避免推断系统中有哪些计划） |
| 限流 | [`security.py`](./python/app/security.py) | 按客户端 IP 令牌桶，超限 `429 RATE_LIMITED` + `Retry-After`（`API_RATE_LIMIT_PER_MINUTE`，默认关闭）。分桶键**只认 TCP 对端地址**：`X-Forwarded-For` 仅在直连对端命中 `API_TRUSTED_PROXIES` 清单时才采信（否则伪造该头即可换桶绕过限流）。注意 `run.py` 已显式关闭 uvicorn 的 `--proxy-headers`（其默认把 `127.0.0.1` 当可信代理并用 XFF 改写 `request.client`，会绕过上述判断）；用 uvicorn CLI 启动时请加 `--no-proxy-headers`。后端可插拔：配置 `REDIS_URL` 时自动切换为 **Redis 共享令牌桶**（判定与扣减在同一段 Lua 内原子完成，桶键带 TTL），`API_WORKERS>1` / 多副本下得到**真正的全局配额**；未配置则为单进程内存实现（全局配额 ≈ 配置值 × worker 数），可用 `RATE_LIMIT_BACKEND=memory/redis` 显式指定 |
| 安全响应头 | [`security.py`](./python/app/security.py) | `X-Content-Type-Options` / `X-Frame-Options` / `Referrer-Policy` 等，含错误与限流响应 |
| 请求体上限 | [`security.py`](./python/app/security.py) | **纯 ASGI** 中间件 `BodySizeLimitMiddleware`（`API_MAX_BODY_BYTES`，默认 32 MiB）：已声明超限 `Content-Length` 时**不读 body** 直接 `413 PAYLOAD_TOO_LARGE`；未声明长度（chunked）时**读到越界块即中断**，绝不把整个 body 读进内存。限流按「请求数/分钟」计量，防不住单请求体积，二者互补 |
| 错误响应脱敏 | [`main.py`](./python/app/main.py)、[`routes/proxy.py`](./python/app/routes/proxy.py) | 500 / 502 只回**稳定错误码 + 通用文案**：异常文本可能带出上游响应、连接串、文件路径，回显等于信息泄漏。traceback 只进服务端结构化日志，调用方凭 `X-Request-Id` 关联排查（该首部在 500 响应上也会补发） |
| 注入消毒 | [`agent/sanitize.py`](./python/app/agent/sanitize.py) | 剥离用户输入中的伪角色前缀与 ChatML 特殊 token；送入模型前用 `<<<USER_INPUT ... USER_INPUT>>>` 显式边界包裹，让模型区分「指令」与「数据」 |
| 结构化日志 | [`logging_config.py`](./python/app/logging_config.py) | 单行 JSON 日志，自动携带 `requestId` |
| 全链路关联 | [`main.py`](./python/app/main.py) | 生成/透传 `X-Request-Id`，同步下发给引擎与前端 |
| 指标 | [`metrics.py`](./python/app/metrics.py)、`GET /metrics` | Prometheus 文本（请求数/耗时直方图/引擎/Agent/WS/缓存/作业/幂等/向量库），纯标准库零依赖；路径标签按路由模板降基数——**未匹配与 catch-all 一律折叠为 `{unmatched}` / `{id}` 形态**，客户端无法通过构造任意路径向 label 注入高基数（曾经 `/metrics` 里出现过 `/api/plans/doeotnotexist123`） |
| 探针 | `GET /healthz`（存活）/ `GET /readyz`（就绪） | 就绪=引擎可达且未在关机；LLM 为可选能力不作门槛 |
| 引擎韧性 | [`resilience.py`](./python/app/resilience.py)、[`engine_client.py`](./python/app/engine_client.py) | 连接/读取分离超时、连接池上限、指数退避重试（仅连接失败与幂等方法的 5xx）、三态断路器；**超时按 RFC 9110 分流**：读/写超时（可达但慢）→ `504 http_response_timeout`，连接失败 → `502`。长耗时写单独使用 `ENGINE_LONG_TIMEOUT_SECONDS` / `ENGINE_JOB_TIMEOUT_SECONDS`，不再被普通接口的 60s 切断 |
| 优雅关机 | [`main.py`](./python/app/main.py) | 关机时置 `shutting_down` → `/readyz` 返回 503，随后**取消并回收后台作业任务**、关闭作业 / 幂等 / 会话 / 缓存 / 连接池（各有独立 `close()`，避免连接泄漏） |
| Agent 增强 | [`agent/runner.py`](./python/app/agent/runner.py)、[`agent/llm.py`](./python/app/agent/llm.py) | 同一消息内**并行**执行工具调用（保序回填）、上下文预算超限自动裁剪最旧历史、工具入参结构校验、LLM 调用重试 |
| Agent 输出护栏 | [`agent/runner.py`](./python/app/agent/runner.py)、[`agent/schemas.py`](./python/app/agent/schemas.py) | **服务端以工具真实结果为唯一事实来源重写答复**（对标 OpenAI Agents SDK `outputGuardrails` tripwire / Pydantic AI `ModelRetry` / LangGraph durable HITL）：① 无「已落库的写工具结果」却出现「已成功规划/创建/更新…」→ 整体替换为确定性说明（有 pending 时明确「**尚未落库，待确认**」），二次仍犯直接收敛不再重试；② 文本出现 `[planId]` / `{planId}` 等占位符或形似 ID 但实际 `planId` 为空 → 剔除并追加「未产生计划 ID」；③ `planId` 已产生而文本是占位符 → 替换为真实 ID。写类工具仍走**两阶段确认**（先登记为待确认、用户确认后才执行）；SSE 与同步两条链路的 `final` 内容**同为权威结果**，前端整段替换流式草稿 |
| 会话存储 | [`sessions.py`](./python/app/sessions.py) | 可插拔：内存（LRU + TTL + 后台清理）或 Redis（多副本共享）；同会话串行化（**Redis 租约锁**：`SET NX PX` + Lua token 校验 + 续租，跨副本互斥，超时 `SESSION_BUSY`） |
| 缓存加速 | [`cache.py`](./python/app/cache.py) | 可插拔（Redis / 进程内存 LRU）：缓存**确定性**的向量化结果（`CachedEmbedder`，含同批去重与在途合并），重复提问直接省掉上游往返；缓存是纯优化，**故障一律 fail-open**，不影响可用性。`/api/health` 的 `python.cache` 透出后端与命中率 |
| 多进程 | `run.py`、`API_WORKERS` | 多 worker 需 `SESSION_STORE=redis` + `JOB_STORE=redis` + 网关层 WS 粘性会话；配置 `REDIS_URL` 后限流与缓存亦自动切换为共享实现（断路器仍为进程内状态，必要时可上移到网关） |

运维端点：`http://<host>/healthz`、`/readyz`、`/metrics`（均免鉴权，便于 k8s 探针与 Prometheus 抓取）。
`/ws` **不在豁免之列**：它与 `/api/**` 同样要求「会话 Cookie 或 `API_TOKEN`」，详见 §10.6 / §10.8 / §10.11。

### 10.9 知识库（RAG）与长期记忆

两项能力均为**可选增强**：向量化模型未配置时自动降级，`/agent/health` 的 `knowledgeEnabled` / `memoryEnabled`
返回 `false`，控制台对应页面展示配置引导，主对话链路不受影响。

**技术选型（当前主流方案）**：向量库 [Chroma](https://www.trychroma.com/)（本地持久化，生产可换远端客户端）；
向量化默认用硅基流动 `BAAI/bge-m3`（OpenAI 兼容 `/embeddings`，**长期免费**，固定 1024 维）；也可换成支持自定义维度的模型
（智谱 `embedding-3`、`Qwen/Qwen3-Embedding` 系列，需开启 `EMBEDDING_SEND_DIMENSIONS`）；
检索用**混合检索**——稠密向量召回 + `BM25`（中文按 unigram + bigram 切分，零分词依赖）→ **RRF 融合**（`k=60`）。

**入库流水线**（`python/app/kb/`）：

```
文件/文本 → 文档解析（md / txt / csv / json / html / pdf / docx）→ 递归字符分块（800 字 / 120 重叠）
        → 批量向量化（每批 10 条，指数退避重试）→ 写入 Chroma（cosine）
```

**检索流水线**：`query` → 向量化 → Chroma `query`（相似度 `1 - distance`）+ BM25 全量打分 → RRF 融合 → top-k。

**长期记忆（Mem0 风格）**：每轮对话结束后异步提取「自包含的一句话」候选（模型抽取，容忍 ```json 围栏与噪声）；
写入前与既有记忆做向量比对，相似度 ≥ `MEMORY_DEDUP_THRESHOLD`(0.92) 视为重复并跳过；召回时按
`相似度 ≥ 0.25` 过滤 → 相关度 + 重要度加权（high +0.12 / medium +0.04）排序 → top-k 作为**独立 system 区块**
注入（记忆在前、知识库在后，主提示保持稳定以利缓存）。记忆可停用 / 删除 / 清空，停用后保留数据但不再召回。

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/kb/stats` | 文档数 / 分块数 / 字符数、分块与检索配置、当前向量化模型 |
| `GET` / `POST` | `/api/kb/documents` | 文档清单 / 以粘贴文本入库 |
| `POST` | `/api/kb/documents/upload` | 上传文件入库（multipart：`file`、`title`、`source`、`tags`） |
| `GET` / `DELETE` | `/api/kb/documents/:docId` | 文档详情（含分块预览）/ 删除文档及其全部分块 |
| `POST` | `/api/kb/search` | 混合检索试跑（`query`、`topK`、`hybrid`），返回命中片段与得分 |
| `GET` | `/api/memories/stats` | 记忆总数 / 启用数 / 按类型分布 / 自动提取开关 |
| `GET` / `POST` | `/api/memories` | 记忆清单（可按 `status` 过滤）/ 手工新增 |
| `POST` | `/api/memories/search` | 语义召回试跑（返回下一轮对话会想起哪些记忆） |
| `PATCH` / `DELETE` | `/api/memories/:memoryId` | 启用 / 停用 / 删除单条记忆 |
| `DELETE` | `/api/memories` | 清空全部记忆 |

Agent 侧新增三个**只读**检索工具（写类工具仍走两阶段确认）：`kb_search`、`kb_list_documents`、`memory_search`；
`POST /api/agent/chat` 的响应新增 `references`（本轮引用的知识库片段）与 `memories`（本轮注入的记忆），
控制台 AI 助手抽屉会以卡片展示，便于用户核对结论依据并在发现记忆跑偏时及时纠偏；
流式链路同样携带（`meta` 首帧与 `final` 收尾帧各回传一次）。

> 运行期数据默认落在 `python/.data/chroma`（已 gitignore）；容器部署请挂载卷以免重建镜像丢库。
> Chroma 为**单进程本地库**：多 worker / 多副本时需改用 Chroma Server 或远端向量库（如 Milvus / pgvector）。

### 10.10 缓存加速（可选，Redis / 进程内存）

配置 `REDIS_URL`（或 `CACHE_BACKEND=redis`）后，服务会把**确定性的昂贵计算结果**放进缓存，
用来压缩单轮问答的上游往返。当前接入点是**向量化结果**（[`cache.py`](./python/app/cache.py) +
`CachedEmbedder`）：

- **为什么能缓存**：同一向量化模型 + 维度 + 文本，必然得到同一向量（缓存键已含模型与维度，
  换模型不会命中旧向量），因此不需要版本化失效，只用 TTL 控制占用；
- **省掉的是什么**：一轮对话中「长期记忆召回」与「知识库检索」会对**同一段用户输入**各向量化
  一次，第二次直接命中缓存；重复提问 / 高并发相同提问则完全省掉上游往返（首字更快）；
- **同批去重 + 在途合并**：同一条消息内重复文本只上行一次；相同批次的并发请求共享同一个
  上游调用，避免缓存击穿时的重复风暴。

设计取舍（与**会话存储**刻意不同，务必区分）：

| 维度 | 会话存储（`sessions.py`） | 缓存（`cache.py`） |
|---|---|---|
| 数据性质 | **业务数据**：丢了就是丢上下文 | **纯优化**：丢了只是重算一次 |
| 后端故障 | 明确失败 `SESSION_STORE_UNAVAILABLE(503)`，绝不静默降级 | **fail-open**：按未命中处理、计数告警，绝不影响可用性 |
| 超时 | 按业务时长设置 | 刻意设短（1s），故障时快速失败 |

配置项：`CACHE_BACKEND`（`auto`/`memory`/`redis`）、`CACHE_TTL_SECONDS`（默认 3600，设 0 关闭缓存）、
`CACHE_MAX_ENTRIES`（内存后端 LRU 上限）、`RATE_LIMIT_BACKEND`（`auto`/`memory`/`redis`）。
可观测：`/api/health` 的 `python.cache` 返回后端、命中率与淘汰次数，`/metrics` 暴露
`cache_operations_total{backend,outcome}`；限流后端故障计数为 `rate_limit_backend_errors_total`。

> 未配置 `REDIS_URL` 时自动退化为进程内 LRU 缓存：单进程部署同样有效，只是命中率不跨副本共享。
> `CACHE_BACKEND=redis` / `RATE_LIMIT_BACKEND=redis` 为**显式选择**，此时缺 `REDIS_URL` 或未安装
> `redis` 依赖会直接报错启动失败，不做静默降级（避免「以为共享了其实各存各的」）。
> 可选 Redis 依赖已单独声明：`python -m pip install -r python/requirements-redis.txt`。

### 10.11 访问控制（登录 + 角色）

> 目标：把「一把全局密钥人人共用」改成「一人一账号 + 按角色授权」，同时保持算法层零改动。

**四档角色（对齐 Plane / OpenProject / Vikunja / Focalboard 的通行做法：固定少量角色 + 权限表驱动）**：

| 角色 | 用户管理 | 计划读写 | Agent 对话 | 写确认 | 知识库 | 长期记忆 | 审计 | 计划可见范围 |
|---|---|---|---|---|---|---|---|---|
| `admin` 管理员 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 全部 |
| `member` 成员 | — | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 全部 |
| `viewer` 只读 | — | 只读 | ✅ | — | 只读 | 只读 | ✅ | 全部 |
| `guest` 访客 | — | 只读 | — | — | — | — | — | **仅被指派计划** |

权限标识（`资源:动作`）与角色映射集中在 [`python/app/constants.py`](./python/app/constants.py)，请求路径 → 所需权限的映射集中在
[`python/app/auth/permissions.py`](./python/app/auth/permissions.py)：新增角色或权限**只改这两处**，不散落到各路由。

**人类登录态 = 服务端会话 + `httpOnly` Cookie**（同源 SPA，`SameSite=lax`）：

- 浏览器**不存任何凭据**（无 localStorage 令牌），因此不存在 XSS「一把梭」拖走长期凭据的问题；
- 服务端可**即时吊销**：管理员停用 / 删除账号或重置口令、用户改自己口令，相关会话立刻失效（无需等待过期）；
- WebSocket 与 SSE 天然受益：浏览器自动携带同源 Cookie，无需再把令牌拼进 URL（避免被代理与访问日志留存）。

**账号生命周期接口**（`/api/auth/**`）：

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| `POST` | `/api/auth/login` | 免鉴权 | 账号口令登录，下发会话 Cookie；失败统一文案，不区分「账号不存在 / 口令错」 |
| `POST` | `/api/auth/logout` | 免鉴权 | 注销当前会话（幂等） |
| `GET` | `/api/auth/session` | 需登录 | 返回当前主体：`userId` / `username` / `role` / `permissions` / `planScope` / `planIds` |
| `GET` | `/api/auth/roles` | 需登录 | 角色与权限矩阵（前端展示 / 排障） |
| `POST` | `/api/auth/password` | 需登录 | 修改自身口令（须校验原口令；成功后吊销该账号全部会话） |
| `GET` `POST` `PATCH` `DELETE` | `/api/auth/users`、`/api/auth/users/{id}` | `user:manage` | 账号清单 / 新建 / 更新（角色 / 状态 / 显示名 / 重置口令）/ 删除 |
| `PUT` | `/api/auth/users/{id}/plans` | `user:manage` | 设置访客可见计划白名单（**全量覆盖**，空数组即「无权查看任何计划」） |

**关键约束**：

- **引导管理员**：账号库为空时按 `AUTH_BOOTSTRAP_ADMIN_USERNAME` 创建；口令留空则随机生成并在启动日志以 WARNING 打印**一次**（不落盘明文、绝不写进响应体）。库非空时该配置被忽略，不会重置既有口令。
- **最后一名管理员保护**：把唯一「启用中的管理员」降权、停用或删除会被拒绝（`400`），否则控制台将永久失去管理入口。
- **计划作用域**：访客的可见计划白名单由 Python 网关维护并在两处生效——列表接口在转发后过滤未授权计划；单计划接口在鉴权中间件按 `404` 拦下（不泄露计划存在性）；WebSocket 亦按白名单过滤事件。
- **账号存储**：`sqlite3`（单文件，WAL 模式），启用唯一索引与事务，避免「同名账号并发创建」这类读改写竞态；全过程经 `asyncio.Lock` + 线程卸载，不阻塞事件循环。
- **多副本部署**：账号库为单文件，多副本请挂载共享卷；会话表会随之共享，任一副本吊销即时全副本生效。若要接企业 SSO，替换 `app/auth/store.py` 即可，算法与前端不受影响。
- **前端收敛不等于授权**：页面与菜单按 `permissions` 隐藏入口（避免无谓 403 往返），但本地篡改只会得到一个能渲染、取不到数据的空页面。

> 生产部署务必开启 HTTPS 并设 `AUTH_COOKIE_SECURE=true`；跨站场景请同时收紧 `AUTH_COOKIE_SAMESITE`。
> 若只是想跑通演示，可临时 `AUTH_ENABLED=false`（等同于全部请求按机器主体放行），**切勿带入生产**。

### 10.12 异步写链路（`Prefer: respond-async` + 作业资源 + SSE 进度）

> **要解决的问题（真机复现）**：`POST /api/plans` 在引擎侧是**同步阻塞**的算法计算（实测 80s+）。
> 网关 `ENGINE_TIMEOUT_SECONDS=60` 先到 → 返回 **502「算法引擎不可达」**，但 **70s 后引擎把计划落了库**：
> 客户端看到失败并可能重试，服务端却已写入了孤儿计划。全链路缺少「长耗时写」的约定。

**设计对齐**（业界通行做法，非自创）：

| 参考 | 借鉴点 |
|---|---|
| Google LRO / Airflow REST / Dify / Superset | 长耗时写不挂连接，改为 `202` + **可轮询的状态资源** |
| RFC 7240 `Prefer` | 客户端显式声明「可以接受受理回执」`Prefer: respond-async` |
| RFC 9110 + IANA Proxy-Status | 可达但慢 → **504** `http_response_timeout`；连不上 → **502**（二者语义不可混用） |
| Stripe / IETF `Idempotency-Key` draft-07 | 同键同载荷 → **回放**；处理中 → **409**；同键异载荷 → **422** |

**协议**（`POST /api/plans`、`POST /api/plans/:planId/replan`）：

```bash
# ① 提交：拿到作业句柄（立即返回，不等待算法）
curl -i -X POST http://localhost:8081/api/plans \
  -H 'Content-Type: application/json' \
  -H 'Prefer: respond-async' \
  -H 'Idempotency-Key: 8f4c…（客户端生成的稳定键）' \
  -d '{"requirement":"订单中心 v2.1","iterationStartDate":"2026-10-20","iterationDeadline":"2026-11-07","devMemberCount":3}'

# HTTP/1.1 202 Accepted
# Location: /api/jobs/3f2a…	  Prefer: respond-async
# {"data":{"jobId":"3f2a…","kind":"plan.create","status":"pending",
#          "pollUrl":"/api/jobs/3f2a…","streamUrl":"/api/jobs/3f2a…/stream","createdAt":1760000000}}

# ② 订阅进度（SSE；也可用 pollUrl 轮询快照）
curl -N http://localhost:8081/api/jobs/3f2a…/stream
# event: progress  data: {"jobId":"3f2a…","status":"running","phase":"engine","message":"…","at":…}
# event: done      data: {"jobId":"3f2a…","status":"succeeded","planId":"…","result":{...}}

# ③ 取消（尽力而为，见下方边界）
curl -X POST http://localhost:8081/api/jobs/3f2a…/cancel
```

**作业状态机**：`pending → running → succeeded | failed | cancelled`；查询 / 订阅**按属主隔离**，
非属主或不存在的作业一律 **404**（不借状态码差异泄露作业是否存在）。`GET /api/health` 的
`python.jobStore` 会透出后端名与 TTL，便于运维核对「是否真的多副本共享」。

**不回 Prefer 的客户端行为不变**（仅超时语义修正为 504），避免破坏既有调用方；
但同步路径会使用 `ENGINE_LONG_TIMEOUT_SECONDS`（默认 600s），不再用普通接口的 60s。

**幂等键**（仅 `POST/PUT/PATCH/DELETE` 且带 `Idempotency-Key` 时生效）：

| 场景 | 结果 |
|---|---|
| 首次请求 | 正常执行，完成后写回放记录（TTL `IDEMPOTENCY_TTL_SECONDS`，默认 24h） |
| 同键 + 同载荷，已完成 | **回放**原状态码与原响应体（含 `Location`），带 `Idempotency-Replayed: true`；**不重复执行**。首执行不带该首部，调用方据此可区分 |
| 同键 + 同载荷，仍在处理 | `409 IDEMPOTENCY_IN_PROGRESS` + `Retry-After: 1` |
| 同键 + 异载荷 | `422 IDEMPOTENCY_KEY_REUSE`（键被错误复用，须换键） |
| 请求失败（4xx/5xx） | 释放键，允许用同一键重试 |
| 幂等存储不可用 | **放行**并在响应标注 `Idempotency-Status: skipped`，计数 `idempotency_total{outcome="degraded"}` |

> 键按主体隔离（`owner:key`），长度上限 255；指纹 = `sha256(method + path + 排序后查询 + body)`，
> 因此查询参数顺序不影响判定，**载荷不同则绝不复用**。
> 首部命名沿用 IETF `draft-ietf-httpapi-idempotency-key-header`：回放用 `Idempotency-Replayed: true`。

**关键边界（务必知晓）**：

- **作业是状态而不是缓存**，因此它**不 fail-open**：存储不可用 → `503 JOB_STORE_UNAVAILABLE`。
  `API_WORKERS>1` 或多副本部署**必须** `JOB_STORE=redis`（否则 A 副本受理、B 副本查不到，客户端会一直轮询到 404）；
  显式填 `redis` 却缺 `REDIS_URL` 或未装依赖会**启动失败**，不静默降级。可选依赖：`python -m pip install -r python/requirements-redis.txt`。
- **取消不保证中断引擎**：Fastify 侧同步处理器无法被客户端断连强制中断，`cancel` 只把作业置为终态
  （不再把结果写回计划）。要真正中断引擎需改 `src/**`，本轮不做（硬约束：算法单一事实来源）。
- **作业过期**：超过 `JOB_TTL_SECONDS` 后快照与进度不可查，SSE 会以 `error` 事件明确收尾，避免客户端无限等待。

**可观测**：`jobs_total{kind,outcome}`、`job_duration_seconds{kind}`、`idempotency_total{outcome}`、
`idempotency_store_errors_total{operation}`（后者只随存储真故障增长，适合直接配告警）、
`kv_store_operations_total{store,operation,outcome}`；审计日志含 `requestId/user/planId/jobId/key/durationMs`，
控制台「新建计划 / 重规划」在提交后会显示**进度条 + 已运行时长 + 取消按钮**。

---

## 十一、已知边界

- **算法口径修正**：§5.2 表格中两处已与需求方确认，其余严格遵循需求文档。
- **LLM 拆解质量**依赖所接入模型；未接入 LLM 时使用规则拆解（按需求关键词映射任务模板），保证离线可运行。
- **扩展接口**（Jira / Git / CI-CD）当前仅为接口占位，尚未实现具体适配器。
- **异步写作业的取消是「逻辑取消」**：`POST /api/jobs/:id/cancel` 只是把作业置为终态、不再把结果写回计划。
  引擎侧这两个接口是 Fastify 同步处理器，客户端断连不会中断其计算（要真正中断需改 `src/**`，与「算法单一事实来源」硬约束冲突）。
  因此取消后引擎可能仍完成计算并落库——这是**已知且明确披露**的边界，不是静默失败：作业与审计日志都会留下完整痕迹（详见 §10.12）。
- **异步作业存储**：内存后端为**单进程**语义，`API_WORKERS>1` / 多副本必须 `JOB_STORE=redis`，
  否则「A 副本受理作业、B 副本查不到」；作业是状态而非缓存，故存储故障**显式失败**（`503 JOB_STORE_UNAVAILABLE`），不 fail-open。
- **知识库与长期记忆为可选增强**：需配置向量化模型（`EMBEDDING_API_KEY` 或复用 `LLM_API_KEY`）后启用，
  未配置时自动降级且主链路不受影响；向量库为**本地 Chroma 单进程库**，多 worker / 多副本部署需改用
  Chroma Server 或远端向量库（Milvus / pgvector），并在容器中挂载持久卷（默认 `python/.data/chroma`）。
- **检索策略**为「混合检索（向量 + BM25 → RRF）+ agentic 检索工具」，**未引入 rerank 精排模型**；
  若需更高精度可接入 `gte-rerank` 等重排服务作为后续扩展点。
- **向量索引自愈**：Chroma 本地持久化索引存在**偶发**的段不可读缺陷（新建集合时约 1% 命中，
  报 `Error creating hnsw segment reader: Nothing found on disk`，且**重试与重开客户端均无效**）。
  [`heal.py`](./python/app/kb/heal.py) 会在此类错误上原地重建集合（读回正文 → 重新向量化 →
  重建 → 写回；存储损坏时 SQLite 中的正文仍可读回，**数据不丢**），并暴露指标
  `vector_store_self_heal_total{collection,outcome}` 与 `{outcome="failed",reason}`，
  便于直接对「RAG 静默降级」告警。
  触发条件严格限定于该错误特征，其它向量库错误一律原样上抛；重建前先完成向量化，
  中途失败不会删除原集合。
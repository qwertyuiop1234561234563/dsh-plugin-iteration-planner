# 批判式复审（第二轮）与落地修改计划

## Context

用户要求「继续站在批判角度审视项目，并根据批判进行修改，尽量参考 GitHub 高 star 项目的实现」。
第一轮已落地：`requirements-redis.txt`、CI 三 job、Redis 集成测试、Chroma 自愈（`app/kb/heal.py`）、
RAG 降级指标、Redis 会话租约锁 / 原子令牌桶限流、SSE 流式输出。

本轮复审对象是第一轮**尚未细查**的模块：`main.py`（中间件与异常处理）、`routes/agent.py`、
`routes/kb.py`、`agent/memory.py`、`models.py`、`security.py`、`metrics.py`、`ws.py`、`llm.py`。

约束不变：**只改** **`python/**`** **与文档/配置**，绝不触碰 `src/**`、`web/**`；算法单一事实来源不动；
每个修复配单测；**未经用户明确指示不提交、不推送**。

***

## 复审结论（已确认的真实缺陷）

### D1 请求体无大小上限 → 进程 OOM / 单请求 DoS

* 证据：`python/` 全仓检索 `Content-Length` / `413` / `max_body` **零命中**。

* [kb.py](file:///e:/go/dsh-plugin-iteration-planner/python/app/routes/kb.py#L94) `data = await file.read()` 把整个上传读进内存，
  而体积校验发生在 [pipeline.py](file:///e:/go/dsh-plugin-iteration-planner/python/app/kb/pipeline.py) `ingest()` 内部 —— **读完才拒绝**。
  所有 JSON 端点（如 `/api/kb/documents` 的 `content` 上限 200 万字符）同样无上限。

* 影响：少量超大请求即可打爆内存；限流按「请求数/分钟」计，防不住这个维度。

* 参考实现：Starlette 生态通用做法 `ContentSizeLimitMiddleware`（纯 ASGI，同时覆盖
  Content-Length 声明与 chunked 传输两种情形）。

### D2 500 响应回显内部异常文本 → 信息泄漏

* 证据：[main.py](file:///e:/go/dsh-plugin-iteration-planner/python/app/main.py#L460) `internal(f"服务内部错误：{exc}")`；
  [agent.py](file:///e:/go/dsh-plugin-iteration-planner/python/app/routes/agent.py#L156) `internal(f"Agent 编排异常：{exc}")`。

* 影响：把上游异常、路径、驱动细节直接返回调用方，与本项目「不泄漏内部堆栈、只暴露稳定错误码」
  的既定目标自相矛盾（同一份代码在 Redis 异常路径上刻意映射为 503，这里却原文回显）。

* 参考实现：Starlette/FastAPI 默认 `ServerErrorMiddleware` 行为 = 服务端记录 traceback、客户端只给通用文案；
  本项目已有 `X-Request-Id` 可做关联，删掉文案里的 `{exc}` 不损失可排查性。

### D3 后台记忆提取任务的异常被丢弃 → 静默失败

* 证据：[memory.py](file:///e:/go/dsh-plugin-iteration-planner/python/app/agent/memory.py#L210-L212)
  `task.add_done_callback(self._tasks.discard)` —— `discard` 只从集合移除，**不读取结果**。
  `extract()` 只捕获 `ApiError`，`asyncio.to_thread(self._store.count, MEMORY_COLLECTION)` 抛出的
  Chroma/IO 异常会逃逸出任务。

* 影响：长期记忆静默写入失败 + asyncio 打印 `Task exception was never retrieved` 噪音；
  与项目已确立的「降级路径必须产出指标」原则不一致。

* 参考实现：mem0 等把后台写入包成受控任务并显式记录失败。

### D4 sessionId 无字符集校验

* 证据：[models.py](file:///e:/go/dsh-plugin-iteration-planner/python/app/models.py#L31) `sessionId: str | None = Field(default=None, max_length=128)`；
  [models.py](file:///e:/go/dsh-plugin-iteration-planner/python/app/models.py#L90) `AgentConfirmRequest.sessionId` 同样只限长度；
  [agent.py](file:///e:/go/dsh-plugin-iteration-planner/python/app/routes/agent.py#L207) `DELETE /agent/sessions/{session_id}` 路径参数无校验。
  该值直接作为会话存储键与日志字段使用。

* 影响：允许控制字符与任意形态的键（键空间污染、日志注入）；而它本身是「持有即代表身份」的凭据，
  却没有格式约束。项目内已有同类白名单先例 `sanitize_plan_id`（[sanitize.py](file:///e:/go/dsh-plugin-iteration-planner/python/app/agent/sanitize.py#L71)）。

* 兼容性已核实：前端 [AgentDrawer.vue](file:///e:/go/dsh-plugin-iteration-planner/web/src/components/AgentDrawer.vue) 只回传服务端下发的 `uuid4().hex`，
  收紧字符集不会破坏既有调用；`src/**`、`web/**` 无需改动。

### D5（低置信，仅记录，本轮不改）`llm.chat_stream` 的 finally 中 await

[llm.py](file:///e:/go/dsh-plugin-iteration-planner/python/app/agent/llm.py#L161) `finally: await ctx.__aexit__(None, None, None)`：
客户端断开导致 SSE 生成器被 `aclose()` 时，GeneratorExit 处理期间的 await 有触发
`RuntimeError: async generator ignored GeneratorExit` 的理论风险。当前无复现证据，
本轮不动，仅在「断开场景 e2e」中观察，若复现再单独处理。

***

## 修改方案

### 1) 请求体大小限制（对应 D1）

* `python/app/errors.py`：新增 `payload_too_large(message, details)` → `413 PAYLOAD_TOO_LARGE`，
  并更新文件顶部错误码清单注释。

* `python/app/security.py`：新增纯 ASGI 中间件 `BodySizeLimitMiddleware`：

  * 优先读 `content-length`，`O(1)` 直接拒绝，不读 body；

  * 缺失/伪造 `content-length`（chunked）时包裹 `receive` 累计字节数，超限即中断读取并返回 413；

  * 通过 `send` 包装保证响应只发一次（避免 `http.response.start` 重复）；

  * 支持 `0` 表示关闭、支持豁免前缀（默认空）；

  * 与既有组件风格一致：`@runtime_checkable` 不必要，只做一个小类 + 类型注解；

  * 复用 `METRICS.inc_counter`，新增 `http_body_rejected_total{path}`。

* `python/app/config.py`：新增 `api_max_body_bytes`（env `API_MAX_BODY_BYTES`，默认 `32 * 1024 * 1024`，
  `0` 关闭），文档注明「应 ≥ 知识库单文档上限 `KB_MAX_DOCUMENT_BYTES` + multipart 开销」。

* `python/app/main.py`：在 `_register_security_headers(app, settings)` **之后**、`_register_observability(app)` **之前**
  插入 `_register_body_limit(app, settings)`。最终中间件栈（外→内）：
  `cors → observability → security_headers → body_limit → rate_limit → auth`，
  使 413 同样带安全首部、被访问日志与 `http_requests_total` 覆盖。

### 2) 停止向客户端回显内部异常（对应 D2）

* `python/app/main.py` `handle_unexpected`：改为 `logger.exception("未处理异常：%s %s", method, path)` +
  返回 `internal()`（通用文案，不含 `{exc}`）；`ApiError`/`RequestValidationError` 分支不变。

* `python/app/routes/agent.py` SSE `except Exception` 分支：保留已有 `logger.exception`，
  把 `internal(f"Agent 编排异常：{exc}")` 改为 `internal()`。

### 3) 消费后台任务异常（对应 D3）

* `python/app/agent/memory.py` `schedule_extract`：把 `add_done_callback(self._tasks.discard)` 换成
  `add_done_callback(self._on_extract_done)`；新增回调：先 `self._tasks.discard(task)`，
  `task.cancelled()` 时忽略；否则 `exc = task.exception()` 非空时
  `logger.warning("后台记忆提取失败：%s", exc)` 并 `METRICS.inc_counter("memory_extract_failed_total", help=...)`
  （与既有 `memory_recall_degraded_total` 同风格）。

### 4) sessionId 白名单（对应 D4）

* `python/app/models.py`：新增模块级 `_SESSION_ID = r"^[A-Za-z0-9_-]{1,128}$"`，
  作用于 `AgentChatRequest.sessionId` 与 `AgentConfirmRequest.sessionId` 的 `pattern` 参数
  （非法输入直接由既有 `RequestValidationError` 处理器转成 400，提示与其它字段一致）。

* `python/app/agent/sanitize.py`：新增 `sanitize_session_id(value) -> str | None`，与 `sanitize_plan_id` 同风格
  （白名单不匹配返回 `None`），并加入 `__all__`。

* `python/app/routes/agent.py`：`reset_session` 先 `sanitize_session_id(session_id)`，为 `None` 时抛
  `not_found(...)`（保持「非法 ID 与不存在同构」的既有语义，避免探测）。

***

## 单测

> 具体测试文件名在实现时用 `Glob python/tests` 确认后落到最贴近的既有文件里（优先追加到已有的
> `test_security.py` / `test_memory*.py` / `test_agent*.py`，不新开碎文件）。

1. **BodySizeLimitMiddleware**：`content-length` 超限 → 413 且 body 为 `{code:"PAYLOAD_TOO_LARGE"}`；
   chunked 无 `content-length` 超限 → 413；未超限透传 200 且业务 body 完整；
   `api_max_body_bytes=0` 时不限制。用 `httpx.ASGITransport` + 最小 ASGI app 直接驱动中间件，不依赖 Chroma/Redis。
2. **500 文案不回显**：构造必然抛异常的路由，断言 500 响应 `code == "INTERNAL_ERROR"` 且
   `message` **不含**哨兵字符串（如 `__BOOM__`），同时服务端日志中有 traceback。
3. **后台任务异常消费**：直接调用 `MemoryService._on_extract_done` 传入已置异常的 Task，
   断言不抛、`memory_extract_failed_total` 增加、任务被移出 `self._tasks`。
4. **sessionId 校验**：`sessionId="bad id!"` → 400（`VALIDATION_ERROR`）；
   `DELETE /agent/sessions/<非法>` → 404；合法 hex → 走通（用既有 fake runner 夹具）。

***

## 验证

```
cd e:\go\dsh-plugin-iteration-planner\python
python -m pytest -q                     # 基线 194 passed，须全部继续通过
python -m compileall -q app run.py
```

补充一次真实端到端自检（临时脚本用完即删）：

* 起 `uvicorn`（限流开启）→ 发送 >限额 的 body，断言 413 且响应含安全首部与 `X-Request-Id`；

* 正常 `/api/agent/chat` 与 `/chat/stream`（SSE）回归，确认既有功能无退化。

`src/**` / `web/**` 不做任何修改；`npm run typecheck`、`npm test`（引擎）无需重跑（未触及引擎），
但会在最后统一复跑一次以确保门禁全绿。

***

## 明确不做

* 不改 `llm.chat_stream` 的 `finally` await（D5，低置信）。

* 不做「按路径差异化 body 限额」——单一配置项已覆盖需求，多档位属过度设计。

* 不新增第三方依赖（`ContentSizeLimitMiddleware` 用标准库 + Starlette 原生 ASGI 协议实现）。

* **不提交、不推送**，等用户明确指示。


"""服务配置（pydantic-settings）。

所有可调项集中于此，避免魔法值散落；环境变量名与 TypeScript 引擎侧保持同名，
便于同一份 ``.env`` 同时驱动引擎与 Python 服务。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

#: 仓库根目录（本文件位于 ``<repo>/python/app/config.py``）
_REPO_ROOT = Path(__file__).resolve().parents[2]

#: 环境变量文件候选（列表中靠后的文件优先级更高）：
#: 1) 仓库根 ``.env``——与 TS 引擎、docker compose 共用同一份配置（单一事实来源）；
#: 2) 当前工作目录下的 ``.env``——本地覆盖；按 README 在 ``python/`` 目录启动时为 ``python/.env``。
#: 之所以要显式写出仓库根路径：``env_file`` 按 CWD 解析，而本服务在 ``python/`` 目录下启动，
#: 只写相对路径 ``.env`` 会读不到仓库根配置，导致根 ``.env`` 里配置的 LLM / 向量化密钥
#: 对 Agent 层完全不生效（Agent 静默退回内置规则问答，用户却以为已启用了大模型）。
_ENV_FILES = (str(_REPO_ROOT / ".env"), ".env")


class Settings(BaseSettings):
    """Python 对外 API 服务配置。"""

    model_config = SettingsConfigDict(
        env_file=_ENV_FILES,
        # 用 utf-8-sig：Windows 上用 PowerShell 5.1（Set-Content -Encoding utf8）或记事本
        # 保存的 .env 会带 UTF-8 BOM，按普通 utf-8 解析时 BOM 会粘在首个键名上，
        # 导致文件里的第一项配置被静默忽略（例如 LLM_MODEL 丢失 → LLM 未启用）。
        env_file_encoding="utf-8-sig",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------ HTTP 服务 ------------------------------
    api_host: str = "0.0.0.0"
    #: 本网关（Python 对外服务）的监听端口，环境变量为 ``GATEWAY_PORT``。
    #:
    #: 刻意**不复用** ``API_PORT``：TS 算法引擎同样读 ``API_PORT``，而两者共用仓库根
    #: 的同一份 ``.env``（单一事实来源）。同名会导致本地联调时引擎与网关都去绑定 8080，
    #: 后者启动失败或抢占端口。docker compose 中各容器分别显式赋值，故不受影响。
    gateway_port: int = 8080
    api_base_path: str = "/api"
    api_ws_path: str = "/ws"
    #: CORS 允许来源；``*`` 表示全部放开，多个来源用英文逗号分隔
    api_cors_origin: str = "*"
    #: Bearer 访问令牌；为空表示不启用鉴权（生产环境务必配置）
    api_token: str = ""
    log_level: str = "info"
    #: 是否注入安全响应头（nosniff / DENY / no-referrer 等）
    api_security_headers: bool = True
    #: 单客户端 IP 每分钟请求上限（令牌桶）；<=0 表示关闭限流
    api_rate_limit_per_minute: int = 0
    #: 限流突发容量（缺省与每分钟上限相同）
    api_rate_limit_burst: int = 0
    #: 可信反向代理地址清单（逗号分隔的 IP / CIDR）；仅当直连对端命中清单时才采信
    #: ``X-Forwarded-For``。留空表示不信任任何转发头（防客户端伪造 IP 绕过限流）。
    api_trusted_proxies: str = ""
    #: 请求体大小上限（字节）；``0`` 表示关闭。
    #: 须 ≥ ``KB_MAX_DOCUMENT_BYTES`` + multipart 编码开销，否则正常上传会被误判为超限。
    api_max_body_bytes: int = 32 * 1024 * 1024
    #: uvicorn 进程数；>1 时需共享会话存储（Redis）与网关层粘性（详见 README）
    api_workers: int = 1

    # ------------------------------ 访问控制（登录 / 角色） ------------------------------
    #: 是否启用「登录 + 角色」鉴权。开启后所有 /api/** 请求需携带会话 Cookie，
    #: 或使用 API_TOKEN（机器 / CI 令牌，视为内部服务账号）。留空 API_TOKEN 且关闭本项时整体不鉴权。
    auth_enabled: bool = True
    #: 账号 / 会话库路径（sqlite3）；留空使用 ``app.constants.default_auth_store_path()``，
    #: 特殊值 ``:memory:`` 表示进程内存库（仅测试 / 演示用，重启即丢）
    auth_store_path: str = ""
    #: 会话 Cookie 名
    auth_session_cookie: str = "ip_session"
    #: 会话有效期（秒）；过期自动失效，可在用户管理中即时吊销
    auth_session_ttl_seconds: int = 8 * 3600
    #: 会话 Cookie 是否仅经 HTTPS 传输（生产环境务必设为 true）
    auth_cookie_secure: bool = False
    #: 会话 Cookie 的 SameSite 策略（lax / strict / none）
    auth_cookie_samesite: str = "lax"
    #: 首次启动时引导管理员的用户名（仅在账号库为空时创建）
    auth_bootstrap_admin_username: str = "admin"
    #: 引导管理员口令；留空则自动生成随机口令并**以告警日志打印一次**，请立即登录后修改
    auth_bootstrap_admin_password: str = ""

    # --------------------------- 内部算法引擎 ---------------------------
    #: TypeScript 引擎（Fastify）基地址；Python 仅作为对外网关转发 REST 调用
    engine_base_url: str = "http://127.0.0.1:8080"
    engine_timeout_seconds: float = 60.0
    #: **长耗时写**的独立读超时（秒）：建计划 / 重规划等算法计算可达数十秒，
    #: 用普通接口的短超时会先到时并产生「网关报失败但引擎已落库」的孤儿写。
    engine_long_timeout_seconds: float = 600.0
    #: 异步作业调用引擎时的读超时（秒）；应为长任务的真实上界，而非短接口预算
    engine_job_timeout_seconds: float = 600.0
    #: 建立连接的超时（快速失败；读取沿用 engine_timeout_seconds）
    engine_connect_timeout_seconds: float = 5.0
    #: 连接池上限 / 保活连接数
    engine_max_connections: int = 100
    engine_max_keepalive_connections: int = 20
    #: 额外重试次数（0 表示不重试；仅对安全场景生效）
    engine_retry_attempts: int = 2
    #: 退避基数与上限（秒）
    engine_retry_base_delay: float = 0.2
    engine_retry_max_delay: float = 2.0
    #: 断路器：连续失败阈值 / 冷却恢复时间（秒）
    engine_breaker_threshold: int = 5
    engine_breaker_recovery_seconds: float = 30.0

    # ------------------------------ 静态资源 ------------------------------
    #: 前端构建产物目录；存在则同域托管 SPA（history 路由回落 index.html）
    web_static_dir: str = ""

    # ------------------------------ 工作区存储 ------------------------------
    #: 治理数据（基线快照 / 人工风险登记册 / 配置备份）存储文件路径；
    #: 为空则使用 ``app.constants.default_store_path()``（``python/.data/workspace-store.json``）
    workspace_store_path: str = ""

    # ------------------------------ Agent 参数 ------------------------------
    #: function calling 循环的最大步数（防止模型死循环调用工具）
    agent_max_steps: int = 8
    #: LLM 采样温度
    agent_temperature: float = 0.2
    #: 对话会话在内存中的保留上限（LRU 淘汰）
    agent_max_sessions: int = 200
    #: 送入模型的上下文预算（字符数；按 ~4 字符/token 估算，超限自动裁剪最旧历史）
    agent_max_context_chars: int = 96_000
    #: 会话空闲过期时间（秒）；<=0 表示不过期
    agent_session_ttl_seconds: int = 3600
    #: 会话存储驱动：memory（默认）| redis
    session_store: str = "memory"
    #: Redis 连接串（session_store=redis 时必填）
    redis_url: str = ""
    #: 会话分布式锁租约 TTL（秒）；持有期间自动续租，需覆盖单次编排最长耗时
    agent_session_lock_ttl_seconds: float = 300.0
    #: 会话分布式锁获取等待上限（秒）；超时以 SESSION_BUSY(409) 明确失败，避免并发覆盖上下文
    agent_session_lock_wait_seconds: float = 20.0
    #: 多角色协作编排（planner/estimator/scheduler/risk/critic）单次最多产出的角色步骤数
    agent_collab_max_steps: int = 6

    # ------------------------ 主动巡检（后台巡检 Agent） ------------------------
    #: 是否启用后台定时巡检：对可见计划做异常信号聚合 + 与上次快照增量比对，经 WS 推送预警。
    #: 首轮巡检**延迟一个周期**执行（不在启动瞬间集中打引擎），因此不影响启动耗时。
    patrol_enabled: bool = True
    #: 巡检周期（秒）。每个被巡检计划需要一次 ``GET /plans/{id}``，故默认 5 分钟；
    #: 调得过短会让后台持续占用引擎连接，影响交互式请求。
    patrol_interval_seconds: float = 300.0
    #: 单轮巡检最多检查的计划数（计划很多时截断，防止把引擎打满）
    patrol_max_plans: int = 20
    #: 同一信号的告警冷却时间（秒）：冷却期内该信号不重复推送（置 0 表示不去重）
    patrol_alert_cooldown_seconds: float = 1800.0
    #: 巡检发现保留时长（秒）与条数上限（供前端「主动预警」列表展示）
    patrol_finding_ttl_seconds: float = 86400.0
    patrol_max_findings: int = 200
    #: 达到该严重度即视为「需立即处理」（前端高亮 / 可据此接告警通道）
    patrol_escalate_severity: str = "critical"

    # ------------------------ 迭代复盘 + 学习 ------------------------
    #: 是否把复盘结论写回长期记忆（闭环「学习」，用于反哺后续规划的估算与风险口径）
    retro_write_memory: bool = True
    #: 一次复盘最多写回的记忆条数
    retro_max_memories: int = 4

    # ------------------------ What-if 沙盘（自然语言仿真） ------------------------
    #: 单次仿真最多接受的「任务级扰动」条数（防止把临时计划改得面目全非而失去可比性）
    whatif_max_task_edits: int = 10

    # ------------------------ MCP Server（把内部能力暴露为标准 MCP 工具） ------------------------
    #: 是否挂载 Streamable HTTP 传输端点（``/api/mcp``）。stdio 传输由
    #: ``python -m app.mcp.stdio`` 独立进程显式启动，不受本开关影响。
    mcp_enabled: bool = True
    #: MCP 服务名（``initialize`` 响应回显，供客户端识别）
    mcp_server_name: str = "iteration-planner"
    #: MCP 专用访问令牌（Bearer）。留空则回落 ``API_TOKEN``；两者都为空且
    #: ``AUTH_ENABLED=false`` 时才允许无凭据访问（仅本地开发）。
    #: 首部令牌采用**常量时间比较**，避免时序侧信道猜解。
    mcp_token: str = ""
    #: 单次 ``tools/call`` 结果体积上限（字符）：MCP 客户端多为本地进程，超大结果
    #: 会撑爆其上下文与内存，超限时截断并标注（不改变工具本身的业务结果）。
    mcp_max_result_chars: int = 200_000
    #: MCP 传输的单个请求体上限（字节）：MCP 端点不经业务 body 中间件的语义校验，
    #: 必须显式设限，防止无界 JSON-RPC 载荷打爆内存。
    mcp_max_body_bytes: int = 2 * 1024 * 1024

    # ------------------------ 持久化检查点 + 断点续跑 ------------------------
    #: 检查点存储后端：auto（有 REDIS_URL 且依赖可用则 Redis，否则进程内存）| memory | redis。
    #: 多副本 / API_WORKERS>1 必须用 redis，否则「重启后继续确认」只在本副本可见。
    checkpoint_store: str = "auto"
    #: 检查点保留时长（秒）：需覆盖「编排 → 用户确认」的最长间隔（默认 7 天）
    checkpoint_ttl_seconds: float = 7 * 86400.0
    #: 进程内存检查点存储的 LRU 上限（条）
    checkpoint_max_entries: int = 2048
    #: 单个检查点的续跑（重跑）次数上限：超过即拒绝，避免「失败 → 无限续跑」放大故障
    checkpoint_max_attempts: int = 3

    # ------------------------ Agent Evals 评测台 ------------------------
    #: 是否启用评测台端点（``/api/evals/**``）与 CLI 门禁
    evals_enabled: bool = True
    #: 是否启用 LLM-as-judge 评分轨（关闭或模型不可用时退化为纯确定性打分）
    evals_judge_enabled: bool = True
    #: 单次评测最多执行的用例数（防止一次请求打爆引擎 / 模型配额）
    evals_max_cases: int = 100
    #: CI 门禁阈值：通过率下限（低于即判定门禁失败）
    evals_gate_pass_rate: float = 0.8
    #: CI 门禁阈值：综合平均分下限
    evals_gate_min_score: float = 0.7

    # ------------------------ 自反思自愈执行循环 ------------------------
    #: 是否启用结构化反思阶段（关闭则退回「只回填错误、不主动纠偏」的传统循环）
    agent_reflect_enabled: bool = True
    #: 同一「工具 + 归因类别」签名的反思重试次数上限（超过即熔断降级）
    agent_reflect_max_retries: int = 2

    # ------------------------ Agent 执行追踪与回放（trace） ------------------------
    #: 是否落库执行轨迹（``/api/agent/traces/**``）：关闭后端点返回空列表，Agent 行为不变
    agent_trace_enabled: bool = True
    #: 轨迹存储后端：auto（有 REDIS_URL 且依赖可用则 Redis，否则进程内存）| memory | redis。
    #: 多副本 / API_WORKERS>1 必须用 redis，否则「在 A 副本跑的轨迹到 B 副本打不开」。
    agent_trace_store: str = "auto"
    #: 轨迹保留时长（秒）：需覆盖「事故复盘 / 评测失败归因」的窗口（默认 3 天）
    agent_trace_ttl_seconds: float = 3 * 86400.0
    #: 进程内存轨迹存储的 LRU 上限（条）
    agent_trace_max_entries: int = 1024

    # ------------------------ 事故 → 回归用例闭环 ------------------------
    #: 是否启用自定义（回归）用例存储与「由失败轨迹沉淀用例」端点
    evals_custom_cases_enabled: bool = True
    #: 自定义用例存储后端：auto | memory | redis（与检查点同源语义）
    evals_custom_case_store: str = "auto"
    #: 自定义用例保留时长（秒）：回归用例要长期生效（默认 180 天）
    evals_custom_case_ttl_seconds: float = 180 * 86400.0
    #: 自定义用例数量上限（防止黄金集被无限追加，评测时长失控）
    evals_max_custom_cases: int = 200

    # ------------------------ 输出溯源与置信度标注 ------------------------
    #: 是否输出溯源报告（逐条数字声明绑定证据 + 未核实计数）
    agent_provenance_enabled: bool = True

    # ------------------------ 动态上下文预算管理 ------------------------
    #: 是否启用分区预算管理（关闭则退回「从最旧回合开始裁剪」的朴素策略）
    agent_budget_enabled: bool = True
    #: 历史回合保留完整内容的最近轮数（更旧的轮次折叠为一行摘要）
    agent_budget_keep_recent_turns: int = 3

    # ------------------------------ 缓存（可选加速） ------------------------------
    #: 缓存后端：auto（配置了 REDIS_URL 且依赖可用则用 Redis，否则进程内存）| memory | redis
    cache_backend: str = "auto"
    #: 进程内存缓存的 LRU 上限（条）
    cache_max_entries: int = 2048
    #: 向量化结果缓存有效期（秒）；0 表示关闭该缓存
    cache_ttl_seconds: float = 3600.0
    #: 限流后端：auto | memory | redis；多副本 / API_WORKERS>1 时用 redis 才能得到全局配额
    rate_limit_backend: str = "auto"

    # ------------------------ 异步作业（长耗时写） ------------------------
    #: 作业存储后端：auto（有 REDIS_URL 且依赖可用则 Redis，否则进程内存）| memory | redis。
    #: 多副本 / API_WORKERS>1 必须用 redis，否则客户端轮询会打到「没有该作业」的副本上。
    job_store: str = "auto"
    #: 作业记录保留时长（秒）：需覆盖「提交 → 客户端取回终态」的最长间隔
    job_ttl_seconds: float = 3600.0
    #: 进程内存作业存储的 LRU 上限（条）
    job_max_entries: int = 1024

    # --------------------------- 幂等键（可选） ---------------------------
    #: 幂等键记录保留时长（秒）。对齐 Stripe 的 24h 口径：足够覆盖客户端重试窗口，
    #: 又不会让键空间无界增长；到期后同键会被视为新请求。
    idempotency_ttl_seconds: float = 86400.0

    # ------------------------------ WS 推送 ------------------------------
    #: 断线重连时向前端重放最近 N 条事件（0 表示关闭）
    ws_replay_size: int = 20
    #: 单客户端单条事件的下发超时（秒）；``0`` 表示不限。超时即断开该慢客户端，
    #: 避免一个不读数据的客户端拖住整个扇出（扇出是并发的，但超时仍需兜底）。
    ws_send_timeout_seconds: float = 5.0
    #: 同时在线客户端上限（``0`` 表示不限）。达上限后新连接以 1013 拒绝，
    #: 防止连接数无界增长耗尽内存。
    ws_max_clients: int = 512

    # --------------------------- LLM 网关（OpenAI 兼容） ---------------------------
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_seconds: float = Field(default=120.0)
    #: LLM 调用额外重试次数（对连接失败 / 5xx 生效）
    llm_retry_attempts: int = 2
    llm_retry_base_delay: float = 0.5

    # ------------------------ 向量化模型（Embedding） ------------------------
    #: 兼容端点基地址；默认指向硅基流动（``BAAI/bge-m3`` 长期免费），
    #: 留空则回落 ``LLM_BASE_URL``（同一端点兼做 chat 与 embeddings，如智谱）
    embedding_base_url: str = "https://api.siliconflow.cn/v1"
    #: 向量化密钥；留空且 ``EMBEDDING_BASE_URL`` 也为空时回落 ``LLM_API_KEY``
    embedding_api_key: str = ""
    #: 向量化模型名（默认硅基流动 ``BAAI/bge-m3``：免费、固定 1024 维、单条上限 8192 tokens）
    embedding_model: str = "BAAI/bge-m3"
    #: 期望输出维度（用于契约校验与缓存键，须与模型实际输出一致；bge-m3 固定 1024）
    embedding_dimensions: int = 1024
    #: 是否在请求体下发 ``dimensions`` 字段：仅支持自定义维度的模型（智谱 ``embedding-3``、
    #: ``Qwen/Qwen3-Embedding`` 系列）需要开启；``bge-m3`` 等固定维度模型不支持该字段，
    #: 下发会被上游判为非法参数（400），故默认关闭。
    embedding_send_dimensions: bool = False
    #: 单次请求的文本条数（上游普遍限制 64 条以内，保守按 10 条分批）
    embedding_batch_size: int = 10
    embedding_timeout_seconds: float = 60.0
    #: 向量化调用额外重试次数（连接失败 / 429 / 5xx 生效）
    embedding_retry_attempts: int = 2
    embedding_retry_base_delay: float = 0.5

    # --------------------------- 知识库（RAG） ---------------------------
    #: 向量库驱动（当前仅 chroma；保留字段便于后续平滑切换）
    vector_store: str = "chroma"
    #: Chroma 持久化目录；留空则使用 ``python/.data/chroma``
    chroma_dir: str = ""
    #: 分块目标字符数 / 相邻块重叠字符数（按字符计，中文场景稳定）
    kb_chunk_size: int = 800
    kb_chunk_overlap: int = 120
    #: 默认召回条数
    kb_top_k: int = 4
    #: 是否启用混合检索（稠密向量 + BM25 稀疏，RRF 融合）
    kb_hybrid: bool = True
    #: RRF 融合平滑常数（经验值 60）
    kb_rrf_k: int = 60
    #: 单文档体积上限（字节）与知识库文档总数上限
    kb_max_document_bytes: int = 20 * 1024 * 1024
    kb_max_documents: int = 500
    #: 文档详情返回的分块预览条数上限（``chunkCount`` 仍为真实总数）。
    #: 单文档可达 ``kb_max_document_bytes``，默认分块参数下可分块上万，全量返回会撑爆单次响应。
    kb_detail_preview_chunks: int = 50
    #: 对话时是否自动把命中的知识库片段注入上下文（agentic 检索工具同时可用）
    kb_auto_inject: bool = True
    #: 自动注入时使用的召回条数（通常小于人工检索）
    kb_inject_top_k: int = 3

    # --------------------------- 长期记忆 ---------------------------
    #: 是否启用长期记忆（记忆提取 + 语义召回）
    memory_enabled: bool = True
    #: 是否自动从每轮对话中提取记忆（关闭则只支持手工新增）
    memory_extract: bool = True
    #: 每轮注入上下文的最大记忆条数
    memory_top_k: int = 4
    #: 去重阈值：新记忆与既有记忆的余弦相似度 ≥ 该值视为重复，跳过写入
    memory_dedup_threshold: float = 0.92
    #: 记忆总量上限（防止无界增长）
    memory_max_items: int = 2000

    @property
    def llm_enabled(self) -> bool:
        """配置了端点与模型即启用 Agent 的模型主路径。

        刻意不强制密钥：自建 OpenAI 兼容网关（vLLM / Ollama 等）通常无鉴权，客户端在
        ``LLM_API_KEY`` 为空时不发送 ``Authorization`` 头，照常可用；反之若强制要求密钥，
        这类部署会被误判为「未启用」而永远走不到模型主路径。云端网关缺密钥会在调用时
        返回 401 并降级到内置规则问答，错误会以 ``LLM_GATEWAY_ERROR`` 明确暴露，不会静默。
        """
        return bool(self.llm_base_url and self.llm_model)

    @property
    def resolved_embedding_base_url(self) -> str:
        """向量化端点：优先独立配置，否则回落 LLM 网关（同一端点兼做 embeddings）。"""
        return self.embedding_base_url or self.llm_base_url

    @property
    def resolved_embedding_api_key(self) -> str:
        """向量化密钥：优先独立配置；仅在未单独指定向量端点时才回落 LLM 密钥。

        回落必须与端点绑定：显式配置了 ``EMBEDDING_BASE_URL`` 说明向量化走的是另一家
        （例如免费的硅基流动与大模型端的智谱并非同一账号），此时沿用 ``LLM_API_KEY``
        会拿 A 家密钥请求 B 家，得到 401 并污染错误指标。因此这种情况下未配置
        ``EMBEDDING_API_KEY`` 即视为「未启用向量化」，降级为纯算法模式。
        """
        if self.embedding_api_key:
            return self.embedding_api_key
        return "" if self.embedding_base_url else self.llm_api_key

    @property
    def embedding_enabled(self) -> bool:
        """向量化三项齐全（端点 + 模型 + 密钥）才启用知识库与长期记忆。"""
        return bool(self.resolved_embedding_base_url and self.embedding_model and self.resolved_embedding_api_key)

    @property
    def embedding_setup_hint(self) -> str:
        """按**当前配置状态**给出「如何启用知识库 / 长期记忆」的可执行指引（直接展示给用户）。

        指引必须与 :attr:`resolved_embedding_api_key` 的**端点绑定**规则保持一致：显式配置了
        ``EMBEDDING_BASE_URL``（默认即指向硅基流动）时只认 ``EMBEDDING_API_KEY``，此时提示用户
        去设置 ``LLM_API_KEY`` 会把用户引向一个必然失败的配置（拿 A 家密钥请求 B 家 → 401），
        文案与真实待补齐项必须一一对应。
        """
        if not self.resolved_embedding_base_url:
            return "请配置 EMBEDDING_BASE_URL（默认 https://api.siliconflow.cn/v1）后重启服务"
        if not self.embedding_model:
            return "请配置 EMBEDDING_MODEL（默认 BAAI/bge-m3，长期免费）后重启服务"
        if self.embedding_api_key:
            return ""
        if self.embedding_base_url:
            return (
                f"端点已指向 {self.embedding_base_url}，请设置 EMBEDDING_API_KEY 后重启服务"
                f"（模型 {self.embedding_model} 长期免费）；"
                "若希望向量化与 LLM 共用同一端点，请把 EMBEDDING_BASE_URL 一并留空以复用 LLM_API_KEY"
            )
        return "请设置 EMBEDDING_API_KEY（未单独配置向量端点时也可用 LLM_API_KEY）后重启服务"

    @property
    def cors_origins(self) -> list[str]:
        """解析后的 CORS 来源列表。"""
        if self.api_cors_origin.strip() == "*":
            return ["*"]
        return [item.strip() for item in self.api_cors_origin.split(",") if item.strip()]

    @property
    def resolved_auth_store_path(self) -> str:
        """账号 / 会话库路径：未显式配置时落到仓库 ``python/.data/auth.db``。"""
        if self.auth_store_path:
            return self.auth_store_path
        from .constants import default_auth_store_path

        return default_auth_store_path()

    @property
    def resolved_cookie_samesite(self) -> str:
        """Cookie SameSite（归一化并校验，非法值静默回落 ``lax``）。"""
        value = (self.auth_cookie_samesite or "lax").strip().lower()
        return value if value in ("lax", "strict", "none") else "lax"

    @property
    def ws_url_suffix(self) -> str:
        """引擎 WebSocket 路径（与 TS 侧保持一致）。"""
        return self.api_ws_path

    @property
    def resolved_patrol_escalate_severity(self) -> str:
        """巡检「需立即处理」阈值（归一化并校验，非法值静默回落 ``critical``）。"""
        from .constants import PATROL_SEVERITIES

        value = (self.patrol_escalate_severity or "").strip().lower()
        return value if value in PATROL_SEVERITIES else "critical"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例配置（便于测试通过依赖覆盖）。"""
    return Settings()
"""服务配置（pydantic-settings）。

所有可调项集中于此，避免魔法值散落；环境变量名与 TypeScript 引擎侧保持同名，
便于同一份 ``.env`` 同时驱动引擎与 Python 服务。
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Python 对外 API 服务配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------ HTTP 服务 ------------------------------
    api_host: str = "0.0.0.0"
    api_port: int = 8080
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
    #: uvicorn 进程数；>1 时需共享会话存储（Redis）与网关层粘性（详见 README）
    api_workers: int = 1

    # --------------------------- 内部算法引擎 ---------------------------
    #: TypeScript 引擎（Fastify）基地址；Python 仅作为对外网关转发 REST 调用
    engine_base_url: str = "http://127.0.0.1:8080"
    engine_timeout_seconds: float = 60.0
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

    # ------------------------------ 缓存（可选加速） ------------------------------
    #: 缓存后端：auto（配置了 REDIS_URL 且依赖可用则用 Redis，否则进程内存）| memory | redis
    cache_backend: str = "auto"
    #: 进程内存缓存的 LRU 上限（条）
    cache_max_entries: int = 2048
    #: 向量化结果缓存有效期（秒）；0 表示关闭该缓存
    cache_ttl_seconds: float = 3600.0
    #: 限流后端：auto | memory | redis；多副本 / API_WORKERS>1 时用 redis 才能得到全局配额
    rate_limit_backend: str = "auto"

    # ------------------------------ WS 推送 ------------------------------
    #: 断线重连时向前端重放最近 N 条事件（0 表示关闭）
    ws_replay_size: int = 20

    # --------------------------- LLM 网关（OpenAI 兼容） ---------------------------
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_seconds: float = Field(default=120.0)
    #: LLM 调用额外重试次数（对连接失败 / 5xx 生效）
    llm_retry_attempts: int = 2
    llm_retry_base_delay: float = 0.5

    # ------------------------ 向量化模型（Embedding） ------------------------
    #: 兼容端点基地址；留空则回落 ``LLM_BASE_URL``（百炼同一端点既做 chat 也做 embeddings）
    embedding_base_url: str = ""
    #: 密钥；留空则回落 ``LLM_API_KEY``
    embedding_api_key: str = ""
    #: 向量化模型名（百炼 text-embedding-v4 支持 2048/1536/1024/768/512/256/128/64 维）
    embedding_model: str = "text-embedding-v4"
    #: 输出维度（需与模型支持的档位一致；默认 1024 兼顾效果与体积）
    embedding_dimensions: int = 1024
    #: 单次请求的文本条数（百炼 text-embedding-v4 单次上限较小，保守按 10 条分批）
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
        """LLM 网关三项齐全才启用 Agent 的模型主路径。"""
        return bool(self.llm_base_url and self.llm_model)

    @property
    def resolved_embedding_base_url(self) -> str:
        """向量化端点：优先独立配置，否则回落 LLM 网关（百炼同一端点）。"""
        return self.embedding_base_url or self.llm_base_url

    @property
    def resolved_embedding_api_key(self) -> str:
        """向量化密钥：优先独立配置，否则回落 LLM 密钥。"""
        return self.embedding_api_key or self.llm_api_key

    @property
    def embedding_enabled(self) -> bool:
        """向量化三项齐全（端点 + 模型 + 密钥）才启用知识库与长期记忆。"""
        return bool(self.resolved_embedding_base_url and self.embedding_model and self.resolved_embedding_api_key)

    @property
    def cors_origins(self) -> list[str]:
        """解析后的 CORS 来源列表。"""
        if self.api_cors_origin.strip() == "*":
            return ["*"]
        return [item.strip() for item in self.api_cors_origin.split(",") if item.strip()]

    @property
    def ws_url_suffix(self) -> str:
        """引擎 WebSocket 路径（与 TS 侧保持一致）。"""
        return self.api_ws_path


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例配置（便于测试通过依赖覆盖）。"""
    return Settings()
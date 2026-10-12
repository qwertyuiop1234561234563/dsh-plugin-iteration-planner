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

    @property
    def llm_enabled(self) -> bool:
        """LLM 网关三项齐全才启用 Agent 的模型主路径。"""
        return bool(self.llm_base_url and self.llm_model)

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
"""FastAPI 应用装配。

对外角色：企业级控制台与外部系统的**唯一入口**。
- ``/api/**``：透明转发到内部算法引擎（TypeScript），契约零改动；
- ``/api/agent/**``：Python 原生 Agent 编排（自然语言 → 计划 / 会话式修改）；
- ``/ws``：把引擎的计划变更事件扇出给前端（支持断线事件重放）；
- ``/``：托管前端 SPA 产物（配置 ``WEB_STATIC_DIR`` 时）。

企业级能力（本层新增）：
- 鉴权：Bearer ``API_TOKEN``（常量时间比较）+ 按客户端 IP 令牌桶限流 / 安全响应头；
- 可观测：JSON 结构化日志 + ``X-Request-Id`` 全链路关联 + ``/metrics``（Prometheus 文本）；
- 韧性：引擎客户端内置重试与断路器；``/healthz``（存活）/``/readyz``（就绪）分离；
- 会话：可插拔存储（内存 TTL / Redis）+ 会话级锁 + 过期清理；
- 加速：可插拔缓存（Redis / 进程内存）缓存向量化结果，降低重复问答的上游往返与首字延迟。

鉴权豁免：``/healthz``、``/readyz``、``/metrics``、``/api/health``、``/docs``、静态资源。
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from . import __version__
from .agent import AgentRunner, MemoryService, OpenAiCompatibleClient, ToolDispatcher
from .cache import build_cache
from .config import Settings, get_settings
from .constants import KB_COLLECTION, MEMORY_COLLECTION, default_chroma_dir, default_store_path
from .engine_client import EngineClient
from .errors import ApiError, bad_request, internal, rate_limited, unauthorized
from .kb import BailianEmbedder, CachedEmbedder, ChromaVectorStore, Embedder, build_knowledge_base
from .logging_config import configure_logging, request_id_var
from .metrics import METRICS
from .resilience import CircuitBreaker, RetryPolicy
from .routes.agent import build_agent_router
from .routes.kb import build_kb_router
from .routes.memory import build_memory_router
from .routes.proxy import register_proxy_and_static
from .routes.scenario import build_scenario_router
from .routes.system import register_system_routes
from .routes.workspace import build_workspace_router
from .security import (
    SECURITY_HEADERS,
    RateLimitBackend,
    build_rate_limiter,
    client_ip,
    constant_time_equals,
    parse_trusted_proxies,
)
from .sessions import build_session_store
from .state import AppContext
from .workstore import WorkspaceStore
from .ws import EngineEventBridge, WsBroadcaster

logger = logging.getLogger("iteration_planner.http")

#: 形如 UUID / 长十六进制的路径片段（指标标签降基数用）
_ID_SEGMENT = re.compile(r"^[0-9a-fA-F]{8,}$|^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def derive_engine_ws_url(engine_base_url: str, ws_path: str) -> str:
    """由引擎 HTTP 基地址推导 WebSocket 地址。"""
    parts = urlsplit(engine_base_url)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, ws_path, "", ""))


def create_app(
    settings: Settings | None = None,
    *,
    engine: EngineClient | None = None,
    llm=None,
    embedder: Embedder | None = None,
    vector_store: ChromaVectorStore | None = None,
    start_bridge: bool = True,
) -> FastAPI:
    """构建 FastAPI 应用。

    :param engine: 注入的自定义引擎客户端（测试用）；缺省则按配置创建。
    :param llm: 注入的自定义 LLM 客户端（测试用）；缺省则按配置创建。
    :param embedder: 注入的自定义向量化客户端（测试用桩实现）；缺省则按配置创建。
    :param vector_store: 注入的自定义向量库（测试用临时目录）；缺省则按配置创建。
    :param start_bridge: 是否启动「引擎事件流 → 前端 WS」的桥接任务（测试可关闭）。
    """
    settings = settings or get_settings()
    resolved_engine = engine or EngineClient(
        settings.engine_base_url,
        settings.api_base_path,
        settings.engine_timeout_seconds,
        connect_timeout=settings.engine_connect_timeout_seconds,
        max_connections=settings.engine_max_connections,
        max_keepalive_connections=settings.engine_max_keepalive_connections,
        retry=RetryPolicy(
            attempts=settings.engine_retry_attempts,
            base_delay=settings.engine_retry_base_delay,
            max_delay=settings.engine_retry_max_delay,
        ),
        breaker=CircuitBreaker(
            failure_threshold=settings.engine_breaker_threshold,
            recovery_timeout=settings.engine_breaker_recovery_seconds,
        ),
    )
    resolved_llm = llm
    if resolved_llm is None and settings.llm_enabled:
        resolved_llm = OpenAiCompatibleClient(
            settings.llm_base_url,
            settings.llm_api_key,
            settings.llm_model,
            settings.llm_timeout_seconds,
            retry=RetryPolicy(
                attempts=settings.llm_retry_attempts,
                base_delay=settings.llm_retry_base_delay,
                max_delay=max(settings.llm_retry_base_delay * 4, 2.0),
            ),
        )

    resolved_embedder = embedder
    #: 只释放自己创建的向量化客户端：注入的实现（测试桩）由调用方负责关闭
    owns_embedder = embedder is None
    if resolved_embedder is None and settings.embedding_enabled:
        resolved_embedder = BailianEmbedder(
            settings.resolved_embedding_base_url,
            settings.resolved_embedding_api_key,
            settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            batch_size=settings.embedding_batch_size,
            timeout=settings.embedding_timeout_seconds,
            retry=RetryPolicy(
                attempts=settings.embedding_retry_attempts,
                base_delay=settings.embedding_retry_base_delay,
                max_delay=max(settings.embedding_retry_base_delay * 4, 2.0),
            ),
        )

    cache = build_cache(
        driver=settings.cache_backend,
        redis_url=settings.redis_url,
        max_entries=settings.cache_max_entries,
    )
    # 向量化结果是确定性计算，包一层缓存：一轮对话中「记忆召回」与「知识库检索」对同一段
    # 用户输入只向量化一次；重复提问 / 多副本共享命中后直接省掉上游往返（首字更快）。
    if resolved_embedder is not None and settings.cache_ttl_seconds > 0:
        resolved_embedder = CachedEmbedder(resolved_embedder, cache, ttl_seconds=settings.cache_ttl_seconds)

    knowledge = None
    memory = None
    resolved_store = vector_store
    if resolved_embedder is not None:
        try:
            if resolved_store is None:
                resolved_store = ChromaVectorStore(
                    settings.chroma_dir or default_chroma_dir(),
                    chunk_collection=KB_COLLECTION,
                    memory_collection=MEMORY_COLLECTION,
                )
            knowledge = build_knowledge_base(
                resolved_store,
                resolved_embedder,
                chunk_size=settings.kb_chunk_size,
                chunk_overlap=settings.kb_chunk_overlap,
                top_k=settings.kb_top_k,
                hybrid=settings.kb_hybrid,
                rrf_k=settings.kb_rrf_k,
                max_document_bytes=settings.kb_max_document_bytes,
                max_documents=settings.kb_max_documents,
            )
            if settings.memory_enabled:
                memory = MemoryService(
                    resolved_store,
                    resolved_embedder,
                    resolved_llm,
                    top_k=settings.memory_top_k,
                    dedup_threshold=settings.memory_dedup_threshold,
                    max_items=settings.memory_max_items,
                    extract_enabled=settings.memory_extract,
                )
        except Exception as exc:  # noqa: BLE001 - 向量库不可用不应阻断主服务（降级为无检索增强）
            logger.error("知识库 / 长期记忆初始化失败，已降级为纯算法模式：%s", exc)
            knowledge = None
            memory = None

    store = build_session_store(
        driver=settings.session_store,
        max_sessions=settings.agent_max_sessions,
        ttl_seconds=settings.agent_session_ttl_seconds,
        redis_url=settings.redis_url,
        lock_ttl_seconds=settings.agent_session_lock_ttl_seconds,
        lock_wait_seconds=settings.agent_session_lock_wait_seconds,
    )
    dispatcher = ToolDispatcher(resolved_engine, knowledge=knowledge, memory=memory)
    runner = AgentRunner(
        resolved_llm,
        dispatcher,
        max_steps=settings.agent_max_steps,
        temperature=settings.agent_temperature,
        store=store,
        max_context_chars=settings.agent_max_context_chars,
        knowledge=knowledge,
        memory=memory,
        kb_auto_inject=settings.kb_auto_inject,
        kb_inject_top_k=settings.kb_inject_top_k,
        memory_top_k=settings.memory_top_k,
    )
    broadcaster = WsBroadcaster(replay_size=settings.ws_replay_size)
    bridge = EngineEventBridge(derive_engine_ws_url(settings.engine_base_url, settings.api_ws_path), broadcaster)
    limiter = build_rate_limiter(
        per_minute=settings.api_rate_limit_per_minute,
        burst=settings.api_rate_limit_burst or None,
        redis_url=settings.redis_url,
        driver=settings.rate_limit_backend,
    )
    workspace = WorkspaceStore(settings.workspace_store_path or default_store_path())

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await store.start()
        await workspace.start()
        if start_bridge:
            bridge.start()
        try:
            yield
        finally:
            # 优雅关机：先置「未就绪」拒绝新请求，再排空后台任务与连接
            _app.state.ctx.shutting_down = True
            if start_bridge:
                await bridge.stop()
            await store.close()
            await workspace.close()
            await cache.close()
            await resolved_engine.aclose()
            if isinstance(resolved_llm, OpenAiCompatibleClient):
                await resolved_llm.aclose()
            if resolved_embedder is not None and owns_embedder:
                await resolved_embedder.aclose()
            await limiter.close()

    app = FastAPI(
        title="企业级研发迭代规划智能 Agent · Python 开放 API",
        version=__version__,
        description=(
            "Python 对外服务：透传内部算法引擎（Kahn 拓扑 + AOE 关键路径 + 风险扫描 + 燃尽图），"
            "并提供基于 LLM function calling 的 Agent 编排能力。底层算法严格由 TypeScript 引擎提供，Python 侧不复制算法。"
        ),
        lifespan=lifespan,
    )
    app.state.ctx = AppContext(
        settings=settings,
        engine=resolved_engine,
        runner=runner,
        broadcaster=broadcaster,
        bridge=bridge,
        metrics=METRICS,
        store=store,
        workspace=workspace,
        started_at=time.monotonic(),
        version=__version__,
        knowledge=knowledge,
        memory=memory,
        embedder=resolved_embedder,
        cache=cache,
        rate_limit=limiter,
    )

    # 注册顺序：先注册的在内层，后注册的在外层（Starlette 语义），CORS 需最外层。
    _register_auth(app, settings)
    _register_rate_limit(app, settings, limiter)
    _register_security_headers(app, settings)
    _register_observability(app)
    _register_exception_handlers(app)
    _register_cors(app, settings)

    register_system_routes(app)
    app.include_router(build_agent_router(settings.api_base_path))
    # 情景对比必须先于 catch-all 静态/代理路由注册（Starlette 先注册先匹配）
    app.include_router(build_scenario_router(settings.api_base_path))
    # 治理能力（导入 / 基线 / 登记册 / 负荷 / 燃尽分析 / 变更历史 / 质量 / 配置备份）
    app.include_router(build_workspace_router(settings.api_base_path))
    # 知识库（RAG）与长期记忆：检索增强能力的治理入口
    app.include_router(build_kb_router(settings.api_base_path))
    app.include_router(build_memory_router(settings.api_base_path))
    register_proxy_and_static(app)
    return app


def _register_cors(app: FastAPI, settings: Settings) -> None:
    origins = settings.cors_origins
    if origins == ["*"]:
        # 与引擎 @fastify/cors origin:true 行为一致：回显请求来源（支持带凭证的跨域）
        app.add_middleware(
            CORSMiddleware,
            allow_origin_regex=".*",
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["X-Request-Id"],
        )
    else:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["X-Request-Id"],
        )


def _register_auth(app: FastAPI, settings: Settings) -> None:
    """Bearer 鉴权中间件（与引擎规则一致，采用常量时间比较防时序侧信道）。"""

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        token = settings.api_token
        if not token:
            return await call_next(request)

        path = request.url.path
        base = settings.api_base_path
        # 非 API 路径（静态资源 / 文档 / 探测端点）不参与鉴权
        if not path.startswith(base):
            return await call_next(request)
        if path.startswith(f"{base}/health") or path.startswith(f"{base}/docs"):
            return await call_next(request)

        header = request.headers.get("authorization", "")
        provided = header[len("Bearer ") :] if header.startswith("Bearer ") else ""
        if not provided or not constant_time_equals(provided, token):
            return JSONResponse(status_code=401, content=unauthorized().to_body())
        return await call_next(request)


def _register_rate_limit(app: FastAPI, settings: Settings, limiter: RateLimitBackend) -> None:
    """按客户端 IP 的令牌桶限流（仅作用于对外 API，豁免探测端点与预检）。

    后端为内存或 Redis（``REDIS_URL`` 时）：Redis 后端让多 worker / 多副本共享**全局**配额，
    避免进程内限流被副本数稀释（详见 :func:`app.security.build_rate_limiter`）。
    """
    base = settings.api_base_path
    counter = {"n": 0}
    #: 仅当直连对端命中该清单时才采信 X-Forwarded-For（防止伪造 IP 绕过限流）
    trusted_proxies = parse_trusted_proxies(settings.api_trusted_proxies)
    #: 仅内存后端需要周期性清理空闲桶；Redis 后端靠键 TTL 自动过期
    prune = getattr(limiter, "prune", None)

    def _should_limit(request: Request) -> bool:
        if request.method == "OPTIONS":
            return False
        path = request.url.path
        if not path.startswith(base):
            return False
        return not (path.startswith(f"{base}/health") or path.startswith(f"{base}/docs"))

    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        if limiter.enabled and _should_limit(request):
            allowed, retry_after = await limiter.allow(client_ip(request, trusted_proxies=trusted_proxies))
            if not allowed:
                METRICS.inc_counter("http_rate_limited_total", {"path": _metric_path(request)})
                response = JSONResponse(status_code=429, content=rate_limited(retry_after=retry_after).to_body())
                response.headers["Retry-After"] = str(retry_after)
                return response
            # 周期性清理空闲桶，避免内存随客户端数量无界增长
            if prune is not None:
                counter["n"] += 1
                if counter["n"] % 1000 == 0:
                    prune()
        return await call_next(request)


def _register_security_headers(app: FastAPI, settings: Settings) -> None:
    """为所有响应注入安全首部（含错误与限流响应）。"""

    @app.middleware("http")
    async def security_headers_middleware(request: Request, call_next):
        response = await call_next(request)
        if settings.api_security_headers:
            for key, value in SECURITY_HEADERS.items():
                response.headers.setdefault(key, value)
        return response


def _register_observability(app: FastAPI) -> None:
    """关联 ID + 访问日志 + HTTP 指标采集（最外层，覆盖全部响应）。"""

    @app.middleware("http")
    async def observability_middleware(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001 - 交由全局异常处理器/ServerErrorMiddleware，仅补埋点
            _record_metrics(request, 500, time.perf_counter() - started)
            logger.exception("请求处理异常：%s %s", request.method, request.url.path)
            raise
        finally:
            request_id_var.reset(token)

        elapsed = time.perf_counter() - started
        _record_metrics(request, response.status_code, elapsed)
        response.headers.setdefault("X-Request-Id", request_id)
        logger.info(
            "请求完成",
            extra={
                "extra_fields": {
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "durationMs": round(elapsed * 1000, 2),
                    "requestId": request_id,
                }
            },
        )
        return response


def _record_metrics(request: Request, status: int, elapsed: float) -> None:
    labels = {"method": request.method, "path": _metric_path(request)}
    METRICS.inc_counter("http_requests_total", {**labels, "status": str(status)}, help="HTTP 请求总数")
    METRICS.observe("http_request_duration_seconds", elapsed, labels, help="HTTP 请求耗时（秒）")


def _metric_path(request: Request) -> str:
    """计算指标用的路径标签：优先路由模板，兜底把 ID 片段归一化以控制基数。"""
    route = request.scope.get("route")
    path_format = getattr(route, "path_format", None)
    if path_format and "{full_path" not in path_format:
        return path_format
    raw = request.scope.get("path", "/")
    return "/".join("{id}" if _ID_SEGMENT.match(part) else part for part in raw.split("/"))


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def handle_api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_body())

    @app.exception_handler(RequestValidationError)
    async def handle_validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        issues = [
            {
                "field": ".".join(str(part) for part in error["loc"][1:]) or "(root)",
                "message": error["msg"],
            }
            for error in exc.errors()
        ]
        return JSONResponse(status_code=400, content=bad_request("请求入参校验失败", issues).to_body())

    @app.exception_handler(Exception)
    async def handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=500, content=internal(f"服务内部错误：{exc}").to_body())


# 模块导入时安装 JSON 日志（若宿主已配置日志处理器则保持不动）
configure_logging()

app = create_app()


__all__ = ["app", "create_app", "derive_engine_ws_url"]
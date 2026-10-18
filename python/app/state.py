"""应用运行时上下文。

把「配置 / 引擎客户端 / Agent 编排器 / 知识库 / 长期记忆 / WS 广播器 / 启动时间」
聚合为一个对象，挂载在 ``app.state.ctx`` 上，供各路由以依赖方式读取。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from .agent import AgentRunner
from .agent.memory import MemoryService
from .auth import AuthStore
from .cache import CacheBackend
from .config import Settings
from .engine_client import EngineClient
from .kb.embedder import Embedder
from .kb.pipeline import KnowledgeBaseService
from .metrics import Metrics
from .security import RateLimitBackend
from .sessions import SessionStore
from .workstore import WorkspaceStore
from .ws import EngineEventBridge, WsBroadcaster


@dataclass
class AppContext:
    """进程级单例上下文。"""

    settings: Settings
    engine: EngineClient
    runner: AgentRunner
    broadcaster: WsBroadcaster
    bridge: EngineEventBridge
    metrics: Metrics
    store: SessionStore
    workspace: WorkspaceStore
    started_at: float
    version: str
    #: 知识库服务；未配置向量化模型时为 ``None``（相关端点返回 503 并说明原因）
    knowledge: KnowledgeBaseService | None = None
    #: 长期记忆服务；未配置向量化模型或显式关闭时为 ``None``
    memory: MemoryService | None = None
    #: 向量化客户端（与知识库 / 记忆共用；关停时统一释放连接）
    embedder: Embedder | None = None
    #: 可插拔缓存后端（Redis / 进程内存）：缓存确定性计算结果，加速重复问答
    cache: CacheBackend | None = None
    #: 限流后端（Redis / 进程内存）：多副本经 Redis 共享全局配额
    rate_limit: RateLimitBackend | None = None
    #: 账号 / 会话存储（登录 + 角色鉴权）；``AUTH_ENABLED=false`` 时为 ``None``
    auth: AuthStore | None = None
    #: 异步作业存储（长耗时写的「状态资源」）：客户端凭 jobId 轮询 / 订阅进度
    jobs: "JobStore | None" = None
    #: 幂等键守卫：让「超时重试」不再变成「重复写」
    idempotency: "IdempotencyGuard | None" = None
    #: 在跑的作业任务句柄（jobId → Task）：仅本副本可见，跨副本取消依靠作业状态位
    job_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    #: 优雅关机标志：置位后 /readyz 返回未就绪，拒绝新请求（在途请求自然排空）
    shutting_down: bool = False

    @property
    def uptime_ms(self) -> int:
        return int((time.monotonic() - self.started_at) * 1000)


__all__ = ["AppContext"]
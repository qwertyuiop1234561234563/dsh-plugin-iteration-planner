"""应用运行时上下文。

把「配置 / 引擎客户端 / Agent 编排器 / WS 广播器 / 启动时间」聚合为一个对象，
挂载在 ``app.state.ctx`` 上，供各路由以依赖方式读取。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .agent import AgentRunner
from .config import Settings
from .engine_client import EngineClient
from .metrics import Metrics
from .sessions import SessionStore
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
    started_at: float
    version: str
    #: 优雅关机标志：置位后 /readyz 返回未就绪，拒绝新请求（在途请求自然排空）
    shutting_down: bool = False

    @property
    def uptime_ms(self) -> int:
        return int((time.monotonic() - self.started_at) * 1000)


__all__ = ["AppContext"]
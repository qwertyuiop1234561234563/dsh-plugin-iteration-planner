"""WebSocket 事件推送。

Python 对外服务在 ``/ws`` 上向浏览器推送计划变更事件，事件来源是内部 TypeScript
引擎的 ``/ws``：本模块建立一条到引擎的常驻连接，把事件原样扇出给全部前端客户端。

契约（与引擎一致）：``{type: connected|plan.created|plan.updated|plan.deleted, planId, at, reason, plan}``。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import deque
from typing import Any

import websockets

from .metrics import METRICS

logger = logging.getLogger("iteration_planner.ws")


def _visible(payload: dict[str, Any], plan_ids: frozenset[str] | None) -> bool:
    """事件对某可见范围是否可见（``plan_ids`` 为 ``None`` 表示不受限）。"""
    if plan_ids is None:
        return True
    plan_id = payload.get("planId")
    return isinstance(plan_id, str) and plan_id in plan_ids


class WsBroadcaster:
    """前端 WebSocket 连接管理与广播（含断线重连事件重放缓冲）。

    并发要点（压测下暴露的真实问题）：

    - **扇出必须并发**：早期实现是「逐个 ``await send``」，一个不读数据的客户端（TCP 接收
      窗口写满）会把整个循环卡住——其余客户端全部延迟，且引擎事件桥的 ``async for`` 也停在
      ``_relay`` 上，进而对引擎连接形成反压（读不动引擎 socket）。现在改为 ``asyncio.gather``
      并发下发，单个慢连接只影响它自己；
    - **同一连接串行发送**：每客户端一把 :class:`asyncio.Lock`。心跳重放（端点内 ``send``）
      与广播可能并发触碰同一 socket，而 Starlette 的 ``send_text`` 对并发调用无保护，
      帧可能交错损坏；
    - **慢客户端兜底**：单条事件下发超过 ``send_timeout`` 即判定该连接不可用并断开，
      避免其长期占用扇出槽位；
    - **连接数上限**：超过 ``max_clients`` 的新连接以 ``1013``（服务端暂不可用）拒绝，
      防止连接数无界增长。
    """

    def __init__(
        self,
        replay_size: int = 0,
        *,
        send_timeout: float = 5.0,
        max_clients: int = 0,
    ) -> None:
        #: 客户端 → 发送锁（保证同一连接上的帧串行下发，不交错）
        self._clients: dict[Any, asyncio.Lock] = {}
        #: 客户端 → 事件可见的计划集合；``None`` 表示不受限（全部计划）
        self._scopes: dict[Any, frozenset[str] | None] = {}
        #: 最近 N 条已广播事件，供新连接补齐断线期间遗漏的变更
        self._replay: deque[dict[str, Any]] = deque(maxlen=max(0, replay_size))
        self._send_timeout = max(0.0, send_timeout)
        self._max_clients = max(0, max_clients)

    @property
    def size(self) -> int:
        return len(self._clients)

    @property
    def max_clients(self) -> int:
        return self._max_clients

    async def connect(self, websocket: Any, *, plan_ids: frozenset[str] | None = None) -> bool:
        """接入客户端；超过上限时拒绝并返回 ``False``（调用方应直接结束处理）。

        :param plan_ids: 该连接可见的计划集合；``None`` 表示不受限。访客（guest）只应
            看到被指派的计划，而 ``/ws`` 推送的是**全量载荷**（含需求原文与任务明细），
            因此在扇出时按此集合过滤，而不是把未授权计划的事件推到连接上。
        """
        await websocket.accept()
        if self._max_clients and len(self._clients) >= self._max_clients:
            METRICS.inc_counter("ws_connections_total", {"outcome": "rejected"})
            logger.warning("WebSocket 连接数已达上限 %d，拒绝新连接", self._max_clients)
            with contextlib.suppress(Exception):
                await websocket.close(code=1013, reason="连接数已达上限，请稍后重试")
            return False
        self._clients[websocket] = asyncio.Lock()
        self._scopes[websocket] = plan_ids
        METRICS.inc_counter("ws_connections_total", {"outcome": "accepted"})
        return True

    def disconnect(self, websocket: Any) -> None:
        self._clients.pop(websocket, None)
        self._scopes.pop(websocket, None)

    def replay_payloads(self, *, plan_ids: frozenset[str] | None = None) -> list[dict[str, Any]]:
        """返回重放缓冲中的事件快照（断线重连时下发，按连接可见范围过滤）。"""
        return [payload for payload in self._replay if _visible(payload, plan_ids)]

    async def send(self, websocket: Any, payload: dict[str, Any]) -> None:
        """向单个客户端下发（经该连接的发送锁串行化，避免与广播并发写同一 socket）。"""
        lock = self._clients.get(websocket)
        text = json.dumps(payload, ensure_ascii=False, default=str)
        if lock is None:  # 未登记的连接（如拒绝后的收尾下发）：直接发，不做加锁
            await websocket.send_text(text)
            return
        async with lock:
            await websocket.send_text(text)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        """向全部客户端**并发**推送；单个客户端异常 / 超时不影响其它客户端。

        受限连接（访客）只收到其被指派计划的事件：无 ``planId`` 的事件（如 ``connected``、
        引擎级广播）同样不下发，避免通过事件类型推断出系统中存在哪些计划。
        """
        self._replay.append(payload)
        clients = list(self._clients.items())
        if not clients:
            return
        scopes = self._scopes
        targets = [(websocket, lock) for websocket, lock in clients if _visible(payload, scopes.get(websocket))]
        if not targets:
            return
        text = json.dumps(payload, ensure_ascii=False, default=str)
        results = await asyncio.gather(*(self._deliver(websocket, lock, text) for websocket, lock in targets))
        for websocket, ok in zip((item[0] for item in targets), results):
            if not ok:
                self.disconnect(websocket)

    async def _deliver(self, websocket: Any, lock: asyncio.Lock, text: str) -> bool:
        """下发一条事件；返回 ``False`` 表示该连接应被清理（已断开或超时）。"""
        try:
            async with lock:
                if self._send_timeout:
                    await asyncio.wait_for(websocket.send_text(text), timeout=self._send_timeout)
                else:
                    await websocket.send_text(text)
        except asyncio.TimeoutError:
            METRICS.inc_counter("ws_broadcast_total", {"outcome": "timeout"})
            logger.warning("客户端下发超时（%.1fs），断开该慢连接", self._send_timeout)
            return False
        except Exception:  # noqa: BLE001 - 连接已断开等，清理即可
            METRICS.inc_counter("ws_broadcast_total", {"outcome": "failed"})
            return False
        METRICS.inc_counter("ws_broadcast_total", {"outcome": "delivered"})
        return True


class EngineEventBridge:
    """把内部引擎的 WebSocket 事件桥接给前端（自动重连）。"""

    def __init__(self, engine_ws_url: str, broadcaster: WsBroadcaster) -> None:
        self._url = engine_ws_url
        self._broadcaster = broadcaster
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="engine-event-bridge")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(self._url, ping_interval=30, ping_timeout=30) as conn:
                    logger.info("已连接内部引擎事件流：%s", self._url)
                    backoff = 1.0
                    async for raw in conn:
                        await self._relay(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 断线重连
                logger.warning("引擎事件流中断（%s），%.0fs 后重连", exc, backoff)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                backoff = min(backoff * 2, 30.0)

    async def _relay(self, raw: Any) -> None:
        """解析引擎消息并扇出；非 JSON 消息忽略。"""
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            return
        if isinstance(payload, dict):
            await self._broadcaster.broadcast(payload)


__all__ = ["WsBroadcaster", "EngineEventBridge"]
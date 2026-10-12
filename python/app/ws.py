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

logger = logging.getLogger("iteration_planner.ws")


class WsBroadcaster:
    """前端 WebSocket 连接管理与广播（含断线重连事件重放缓冲）。"""

    def __init__(self, replay_size: int = 0) -> None:
        self._clients: set[Any] = set()
        #: 最近 N 条已广播事件，供新连接补齐断线期间遗漏的变更
        self._replay: deque[dict[str, Any]] = deque(maxlen=max(0, replay_size))

    @property
    def size(self) -> int:
        return len(self._clients)

    async def connect(self, websocket: Any) -> None:
        await websocket.accept()
        self._clients.add(websocket)

    def disconnect(self, websocket: Any) -> None:
        self._clients.discard(websocket)

    def replay_payloads(self) -> list[dict[str, Any]]:
        """返回重放缓冲中的事件快照（断线重连时下发）。"""
        return list(self._replay)

    async def send(self, websocket: Any, payload: dict[str, Any]) -> None:
        await websocket.send_text(json.dumps(payload, ensure_ascii=False, default=str))

    async def broadcast(self, payload: dict[str, Any]) -> None:
        """向全部客户端推送；单个客户端异常不影响其它客户端。"""
        self._replay.append(payload)
        if not self._clients:
            return
        text = json.dumps(payload, ensure_ascii=False, default=str)
        dead: list[Any] = []
        for websocket in list(self._clients):
            try:
                await websocket.send_text(text)
            except Exception:  # noqa: BLE001 - 连接已断开等，清理即可
                dead.append(websocket)
        for websocket in dead:
            self.disconnect(websocket)


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
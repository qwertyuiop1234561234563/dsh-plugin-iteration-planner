"""压力 / 冒烟测试：并发、极端输入与慢客户端。

与既有功能用例的区别：这里**不验证 happy path**，而是刻意制造压力场景，检查
「并发下是否仍正确」「极端输入是否可控退化」「慢/坏连接是否拖垮整体」。

覆盖的极端场景：

- **并发 SSE 流**：多路流并行下发，事件序列与会话彼此不串扰；
- **同会话并发**：会话锁必须串行化，历史不得被并发覆盖（力扣式「丢历史」回归）；
- **并发限流**：并发打到同一 IP，配额不得被并发穿透（内存后端临界区内无 await）；
- **WS 慢客户端**：扇出必须并发，单机慢连接不得拖住其余客户端（旧实现逐个 await 会被卡死）；
- **会话锁登记表**：大量不同会话时必须有界；``pop()`` 不得在锁被持有时换掉锁对象
  （否则同一会话会出现两把锁 → 互斥静默失效）；
- **极端输入**：消息 / 检索词 / 文档 / 文档数达到与超过上限，超大文档分块后的检索仍可用。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.constants import SSE_FINAL, SSE_META
from app.main import create_app
from app.sessions import InMemorySessionStore
from app.ws import WsBroadcaster

from .conftest import ScriptedLlm, StreamingScriptedLlm, StubEmbedder, auth

#: 会话 ID 必须命中白名单（``^[A-Za-z0-9_-]{1,128}$``）
SESSION_ID = "deadbeefcafe0001"


def _client_for(
    settings: Settings,
    engine: Any,
    llm: Any = None,
    *,
    overrides: dict[str, Any] | None = None,
    embedder: Any | None = None,
    vector_store: Any | None = None,
) -> tuple[httpx.AsyncClient, Any]:
    """构建「同事件循环内」的异步客户端（并发用例必需；TestClient 起线程池会掩盖竞态）。"""
    cfg = settings.model_copy(update=overrides or {})
    app = create_app(cfg, engine=engine, llm=llm, embedder=embedder, vector_store=vector_store, start_bridge=False)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://stress.test",
        timeout=120.0,
    )
    return client, app


def _sse_frames(text: str) -> list[tuple[str, dict[str, Any]]]:
    """解析 SSE 文本为 ``[(event, data)]``。"""
    frames: list[tuple[str, dict[str, Any]]] = []
    for block in text.split("\n\n"):
        event = None
        data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[len("event: ") :].strip()
            elif line.startswith("data: "):
                data = line[len("data: ") :].strip()
        if event and data:
            frames.append((event, json.loads(data)))
    return frames


# =============================== 并发 SSE 流 ===============================


async def test_concurrent_sse_streams_are_isolated(settings: Settings, engine: Any):
    """20 路并发 SSE：每路恰好一次 ``meta`` + 一次 ``final``，会话 ID 互不相同。"""
    count = 20
    llm = StreamingScriptedLlm([{"role": "assistant", "content": f"回答-{index}"} for index in range(count)])
    client, _ = _client_for(settings, engine, llm)
    try:
        responses = await asyncio.gather(
            *(
                client.post("/api/agent/chat/stream", json={"message": f"问题-{index}"}, headers=auth())
                for index in range(count)
            )
        )
    finally:
        await client.aclose()

    assert [response.status_code for response in responses] == [200] * count
    session_ids: set[str] = set()
    for response in responses:
        frames = _sse_frames(response.text)
        events = [event for event, _ in frames]
        assert events.count(SSE_META) == 1, f"meta 事件应恰好一次：{events}"
        assert events.count(SSE_FINAL) == 1, f"final 事件应恰好一次：{events}"
        assert events[-1] == SSE_FINAL, f"final 必须是最后一个事件：{events}"
        meta = next(data for event, data in frames if event == SSE_META)
        session_ids.add(meta["sessionId"])
    assert len(session_ids) == count, "每路会话必须独立，会话 ID 不得复用"


async def test_concurrent_streams_on_same_session_stay_consistent(settings: Settings, engine: Any):
    """同一会话 5 路并发流：串行化后历史应为 5 轮 × 2 条，不丢不重。"""
    from app.constants import SSE_FINAL as FINAL

    rounds = 5
    llm = StreamingScriptedLlm([{"role": "assistant", "content": f"第{index}轮回复"} for index in range(rounds)])
    client, app = _client_for(settings, engine, llm)
    try:
        responses = await asyncio.gather(
            *(
                client.post(
                    "/api/agent/chat/stream",
                    json={"message": f"第{index}轮提问", "sessionId": SESSION_ID},
                    headers=auth(),
                )
                for index in range(rounds)
            )
        )
        history = await app.state.ctx.store.get(SESSION_ID)
    finally:
        await client.aclose()

    assert all(response.status_code == 200 for response in responses)
    assert all(dict(_sse_frames(response.text)).get(FINAL) for response in responses)
    assert history is not None
    assert len(history) == rounds * 2, f"历史被并发覆盖（应为 {rounds * 2} 条，实为 {len(history)}）"
    assert [message["role"] for message in history] == ["user", "assistant"] * rounds


async def test_concurrent_same_session_chat_keeps_history(settings: Settings, engine: Any):
    """非流式同会话 8 路并发：历史条数精确等于 8 轮 × 2 条。"""
    rounds = 8
    llm = ScriptedLlm([{"role": "assistant", "content": f"回复{index}"} for index in range(rounds)])
    client, app = _client_for(settings, engine, llm)
    try:
        responses = await asyncio.gather(
            *(
                client.post(
                    "/api/agent/chat",
                    json={"message": f"提问{index}", "sessionId": SESSION_ID},
                    headers=auth(),
                )
                for index in range(rounds)
            )
        )
        history = await app.state.ctx.store.get(SESSION_ID)
    finally:
        await client.aclose()

    assert all(response.status_code == 200 for response in responses), [r.status_code for r in responses]
    assert history is not None and len(history) == rounds * 2, f"历史条数异常：{history and len(history)}"


# =============================== 并发限流 ===============================


async def test_concurrent_rate_limit_cannot_be_bypassed(settings: Settings, engine: Any):
    """60 路并发打到同一 IP：放行数不得超过令牌桶容量（并发不得穿透配额）。"""
    capacity = 20
    llm = ScriptedLlm([{"role": "assistant", "content": "ok"} for _ in range(10)])
    client, _ = _client_for(
        settings,
        engine,
        llm,
        overrides={
            "api_rate_limit_per_minute": capacity,
            "api_rate_limit_burst": capacity,
            "rate_limit_backend": "memory",
        },
    )
    try:
        responses = await asyncio.gather(
            *(
                client.post("/api/plans", json={"requirement": f"需求{index}"}, headers=auth())
                for index in range(60)
            )
        )
    finally:
        await client.aclose()

    allowed = [r for r in responses if r.status_code != 429]
    limited = [r for r in responses if r.status_code == 429]
    # 桶容量 20，测试耗时 ≪ 1 分钟，最多额外补入 1 个令牌
    assert len(allowed) <= capacity + 1, f"并发穿透配额：放行 {len(allowed)}"
    assert len(limited) >= 60 - capacity - 1
    assert all(r.headers.get("Retry-After") for r in limited), "429 必须带 Retry-After"


# =============================== WS 扇出 / 慢客户端 ===============================


class _FakeSocket:
    """记录收到帧的假 WebSocket；可模拟「不读数据」的慢客户端。"""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.received: list[str] = []
        self.accepted = False
        self.closed: list[int] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def accept(self) -> None:
        self.accepted = True

    async def send_text(self, text: str) -> None:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.received.append(text)
        finally:
            self.in_flight -= 1

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed.append(code)


async def test_ws_broadcast_is_concurrent_and_drops_slow_client():
    """扇出必须并发：一个 5s 不读数据的客户端不得拖住其余客户端，自身应被超时清理。"""
    broadcaster = WsBroadcaster(replay_size=0, send_timeout=0.2)
    slow = _FakeSocket(delay=5.0)
    fast = [_FakeSocket() for _ in range(3)]
    for socket in [slow, *fast]:
        assert await broadcaster.connect(socket) is True
    assert broadcaster.size == 4

    started = time.monotonic()
    await broadcaster.broadcast({"type": "plan.updated", "planId": "p1"})
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, f"扇出被慢客户端阻塞（耗时 {elapsed:.2f}s）"
    assert all(len(socket.received) == 1 for socket in fast), "正常客户端必须都收到事件"
    assert broadcaster.size == 3, "超时的慢客户端必须被清理"


async def test_ws_same_connection_sends_are_serialized():
    """同一连接上的并发下发必须串行（否则帧会交错损坏）。"""
    broadcaster = WsBroadcaster(replay_size=0, send_timeout=5.0)
    socket = _FakeSocket(delay=0.03)
    assert await broadcaster.connect(socket) is True

    await asyncio.gather(
        broadcaster.send(socket, {"type": "connected"}),
        broadcaster.broadcast({"type": "plan.created", "planId": "p1"}),
        broadcaster.broadcast({"type": "plan.updated", "planId": "p1"}),
    )

    assert socket.max_in_flight == 1, "同一连接上出现了并发写（帧可能交错）"
    assert len(socket.received) == 3


async def test_ws_rejects_connections_beyond_capacity():
    """连接数达到上限后新连接以 1013 拒绝，且不进入客户端集合。"""
    broadcaster = WsBroadcaster(replay_size=0, max_clients=2)
    first, second, third = _FakeSocket(), _FakeSocket(), _FakeSocket()
    assert await broadcaster.connect(first) is True
    assert await broadcaster.connect(second) is True
    assert await broadcaster.connect(third) is False
    assert broadcaster.size == 2
    assert third.closed == [1013], "超限连接必须以 1013 关闭"

    await broadcaster.broadcast({"type": "plan.created", "planId": "p1"})
    assert third.received == [], "被拒连接不得再收到任何事件"


# =============================== 会话存储：极值 ===============================


async def test_session_store_lru_and_lock_registry_are_bounded():
    """会话数受 LRU 上限约束；锁登记表不得随会话数无界增长。"""
    store = InMemorySessionStore(max_sessions=10, ttl_seconds=3600, max_locks=64)
    for index in range(500):
        await store.set(f"session-{index}", [{"role": "user", "content": "x"}])
        store.lock(f"session-{index}")

    assert await store.size() == 10, "会话数必须被 LRU 上限约束"
    assert len(store._locks) <= 64, f"锁登记表无界增长：{len(store._locks)}"


async def test_pop_does_not_replace_a_held_lock():
    """``pop()`` 在锁被持有时不得丢弃该锁对象，否则同一会话会出现两把锁、互斥静默失效。"""
    store = InMemorySessionStore()
    await store.set(SESSION_ID, [])
    order: list[str] = []

    async def holder() -> None:
        async with store.lock(SESSION_ID):
            order.append("hold-enter")
            await asyncio.sleep(0.05)
            order.append("hold-exit")

    async def popper() -> None:
        await asyncio.sleep(0.01)
        await store.pop(SESSION_ID)

    async def contender() -> None:
        await asyncio.sleep(0.02)
        async with store.lock(SESSION_ID):
            order.append("contend-enter")

    await asyncio.gather(holder(), popper(), contender())

    assert order == ["hold-enter", "hold-exit", "contend-enter"], f"互斥被打破：{order}"


async def test_aborted_stream_releases_session_lock(settings: Settings, engine: Any):
    """SSE 中途断开（客户端刷新 / 关闭页面）必须释放会话锁。

    锁泄漏会让该会话**永久阻塞**：后续任何请求都排在已无人持有的锁上，最终全部超时。
    这里直接驱动生成器并在首个事件后 ``aclose()``（即端点 ``finally`` 的真实动作），
    再验证同一会话的下一个请求能立即完成。
    """
    llm = StreamingScriptedLlm([{"role": "assistant", "content": "回复"}])
    client, app = _client_for(settings, engine, llm)
    try:
        pipeline = app.state.ctx.runner.run_chat_stream(SESSION_ID, "问题")
        first = await pipeline.__anext__()
        assert first["event"] == SSE_META
        lock = app.state.ctx.store.lock(SESSION_ID)
        assert lock.locked(), "流式编排进行中必须持有会话锁"

        await pipeline.aclose()  # 模拟端点 finally：终止上游编排
        assert not lock.locked(), "客户端断开后会话锁未被释放（该会话将永久阻塞）"

        # 断开后同一会话必须能立即继续（锁泄漏时此处会等到超时）
        follow_up = await asyncio.wait_for(
            client.post(
                "/api/agent/chat",
                json={"message": "继续", "sessionId": SESSION_ID},
                headers=auth(),
            ),
            timeout=5.0,
        )
        assert follow_up.status_code == 200, follow_up.text[:200]
    finally:
        await client.aclose()


async def test_stream_heartbeat_does_not_cancel_slow_step(settings: Settings, engine: Any, monkeypatch: Any):
    """上游单步慢于心跳间隔时必须持续发心跳，且**不得取消**该步或提前释放会话锁。

    心跳若被实现成「超时即取消进行中的编排」，会中断 LLM / 引擎调用并提前释放会话锁
    （同一会话的请求便可并发写历史）。本用例锁死「心跳只是注释行、编排照常完成」。
    """
    monkeypatch.setattr("app.routes.agent.SSE_HEARTBEAT_SECONDS", 0.05)

    class _SlowLlm(StreamingScriptedLlm):
        """首帧前的模型调用耗时远超心跳间隔。"""

        async def chat_stream(self, messages, *, tools=None, temperature=0.2):
            await asyncio.sleep(0.4)
            async for item in super().chat_stream(messages, tools=tools, temperature=temperature):
                yield item

    llm = _SlowLlm([{"role": "assistant", "content": "慢慢回答的内容"}])
    client, app = _client_for(settings, engine, llm)
    started = time.monotonic()
    try:
        response = await client.post("/api/agent/chat/stream", json={"message": "提问"}, headers=auth())
    finally:
        await client.aclose()
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert response.text.count(": ping") >= 2, "慢步期间必须持续发送心跳注释行"
    frames = _sse_frames(response.text)
    meta = next(data for event, data in frames if event == SSE_META)
    final = next(data for event, data in frames if event == SSE_FINAL)
    assert final["content"] == "慢慢回答的内容", "慢步被心跳取消（模型结果不完整）"
    assert elapsed >= 0.35, f"编排被提前中断（仅耗时 {elapsed:.2f}s）"
    assert not app.state.ctx.store.lock(meta["sessionId"]).locked(), "编排完成后会话锁必须释放"


# =============================== 极端输入 ===============================


def test_extreme_message_length_boundaries(settings: Settings, engine: Any):
    """消息长度：恰好达上限可用；超 1 字符即 400（而不是把超大输入送进模型）。"""
    llm = ScriptedLlm([{"role": "assistant", "content": "ok"}, {"role": "assistant", "content": "ok"}])
    from fastapi.testclient import TestClient

    app = create_app(settings, engine=engine, llm=llm, start_bridge=False)
    with TestClient(app) as client:
        at_limit = client.post("/api/agent/chat", json={"message": "需" * 20_000}, headers=auth())
        assert at_limit.status_code == 200

        over = client.post("/api/agent/chat", json={"message": "需" * 20_001}, headers=auth())
        assert over.status_code == 400
        assert over.json()["code"] == "VALIDATION_ERROR"


def test_extreme_kb_query_length_boundaries(kb_client: Any):
    """检索词上限 2000：达上限可用，超出即 400（避免超长查询拖垮 BM25 / 向量化）。"""
    with kb_client() as client:
        assert client.post("/api/kb/search", json={"query": "订" * 2000}, headers=auth()).status_code == 200
        over = client.post("/api/kb/search", json={"query": "订" * 2001}, headers=auth())
        assert over.status_code == 400
        assert over.json()["code"] == "VALIDATION_ERROR"


def test_extreme_document_size_and_count_limits(kb_client: Any):
    """单文档体积 / 文档总数超限时必须明确拒绝（且不是 500）。

    上限在服务构建时即被 ``KnowledgeBaseService`` 捕获，因此必须经 ``overrides`` 注入，
    而非测试中改 ``settings``（后者不生效——这也正是本用例要防的「配置与实例不一致」）。
    """
    with kb_client(overrides={"kb_max_document_bytes": 1024}) as client:
        too_big = client.post(
            "/api/kb/documents",
            json={"title": "超限", "content": "内容" * 512},
            headers=auth(),
        )
        assert too_big.status_code == 400, too_big.text[:200]
        assert "体积超限" in too_big.json()["message"]

    with kb_client(overrides={"kb_max_documents": 1}) as client:
        first = client.post("/api/kb/documents", json={"title": "第一篇", "content": "灰度发布预案"}, headers=auth())
        assert first.status_code == 200, first.text[:200]
        too_many = client.post("/api/kb/documents", json={"title": "第二篇", "content": "回滚预案"}, headers=auth())
        assert too_many.status_code == 400, too_many.text[:200]
        assert "上限" in too_many.json()["message"]


def test_extreme_document_produces_many_chunks_and_stays_searchable(kb_client: Any):
    """超大文档（分块数上千）仍能入库并检索，且返回条数受 top_k 约束。"""
    body = "\n\n".join(f"第{index}节：订单中心灰度发布与回滚预案" for index in range(4000))
    with kb_client(overrides={"kb_chunk_size": 100, "kb_chunk_overlap": 20}) as client:
        created = client.post(
            "/api/kb/documents",
            json={"title": "大体量文档", "content": body, "tags": ["压测"]},
            headers=auth(),
        )
        assert created.status_code == 200, created.text[:300]
        document = created.json()["data"]
        assert document["chunkCount"] > 500, f"分块数异常偏低：{document['chunkCount']}"

        hits = client.post("/api/kb/search", json={"query": "灰度发布回滚", "topK": 5}, headers=auth())
        assert hits.status_code == 200
        items = hits.json()["data"]["items"]
        assert 0 < len(items) <= 5, "检索条数必须受 topK 约束"
        assert any("灰度发布" in item["text"] for item in items)

        detail = client.get(f"/api/kb/documents/{document['docId']}", headers=auth())
        assert detail.status_code == 200, detail.text[:300]
        assert len(detail.json()["data"]["chunks"]) <= 50, "详情分块预览必须有界（不能全量返回上千块）"
        assert detail.json()["data"]["chunkCount"] == document["chunkCount"], "真实分块总数不得因预览截断而失真"


def test_extreme_empty_and_whitespace_inputs_rejected(kb_client: Any):
    """空 / 纯空白输入必须明确拒绝，不得静默入库空文档。"""
    with kb_client() as client:
        blank = client.post("/api/kb/documents", json={"title": "空白", "content": "   "}, headers=auth())
        assert blank.status_code == 400
        # 校验层（Pydantic）比「分块后为空」更早、更准确地拦住空白正文：
        # 顶层 message 是统一的「请求入参校验失败」，具体原因在 details 里按字段给出。
        details = blank.json()["details"]
        assert any(item["field"] == "content" for item in details), details
        assert any("空" in item["message"] or "空白" in item["message"] for item in details), details

        upload = client.post(
            "/api/kb/documents/upload",
            files={"file": ("empty.md", b"", "text/markdown")},
            headers=auth(),
        )
        assert upload.status_code == 400
        assert "空" in upload.json()["message"]


async def test_invisible_characters_are_stripped_before_dispatch(settings: Settings, engine: Any):
    """控制字符 / 零宽 / 双向覆盖字符必须在进入编排前被剥离。

    这些字符在提示、日志与前端渲染中不可见：``\\x00`` 污染会话存储与结构化日志，
    零宽字符可插进 ``system:`` 中间绕过角色前缀匹配，双向覆盖符（U+202E）会反转显示
    顺序造成「模型看到的内容与前端呈现不一致」。
    """
    llm = ScriptedLlm([{"role": "assistant", "content": "ok"}])
    client, app = _client_for(settings, engine, llm)
    raw = "灰\u200b度\u202e发布\x00预案\x07，正常需求"
    try:
        response = await client.post(
            "/api/agent/chat", json={"message": raw, "sessionId": SESSION_ID}, headers=auth()
        )
        history = await app.state.ctx.store.get(SESSION_ID)
    finally:
        await client.aclose()

    assert response.status_code == 200, response.text[:200]
    assert history is not None and history, "会话历史不应为空"
    stored = history[0]["content"]
    assert stored == "灰度发布预案，正常需求", f"不可见字符未被剥离：{stored!r}"
    assert not any(ord(ch) < 32 and ch not in "\t\n\r" for ch in stored)


async def test_input_without_visible_content_is_rejected(settings: Settings, engine: Any):
    """纯空白 / 纯不可见字符的输入必须 400。

    ``min_length=1`` 只约束原始字符串：``"   "`` 能通过校验但消毒后为空。若照原样放行，
    会话历史会留下空 ``content`` 的用户消息，多数 OpenAI 兼容服务端随后会拒绝该会话的
    所有请求（整条会话不可用），因此必须在入口拦截。
    """
    llm = ScriptedLlm([{"role": "assistant", "content": "ok"}])
    client, _ = _client_for(settings, engine, llm)
    try:
        for raw in ["   ", "\n\t", "\x00\x07", "\u200b\u202e"]:
            response = await client.post(
                "/api/agent/chat", json={"message": raw, "sessionId": SESSION_ID}, headers=auth()
            )
            assert response.status_code == 400, f"{raw!r} 应被拒绝，实为 {response.status_code}"
            assert response.json()["code"] == "VALIDATION_ERROR"

        streamed = await client.post("/api/agent/chat/stream", json={"message": "  "}, headers=auth())
        assert streamed.status_code == 400, "流式端点同样必须拒绝空指令"
        assert streamed.json()["code"] == "VALIDATION_ERROR"

        planned = await client.post("/api/agent/plan", json={"requirement": " \x00"}, headers=auth())
        assert planned.status_code == 400, "端到端规划同样必须拒绝空需求"
        assert planned.json()["code"] == "VALIDATION_ERROR"
    finally:
        await client.aclose()


# =============================== 检索热路径：规模冒烟 ===============================


async def test_bm25_search_scales_to_large_corpus():
    """BM25 在万级分块语料上的单次检索耗时冒烟（防 O(N²) 级退化）。

    阈值故意宽松（2s）：目的是捕捉**量级**退化，而不是做精确性能断言（CI 机器差异大）。
    """
    from app.kb.retrieval import bm25_search

    rows = [
        {
            "id": f"row-{index}",
            "document": f"第{index}节：订单中心灰度发布与回滚预案，包含发布窗口、回滚阈值与联系人。" * 3,
            "metadata": {"docId": f"doc-{index % 50}", "chunkIndex": index},
        }
        for index in range(8000)
    ]
    started = time.monotonic()
    hits = await asyncio.to_thread(bm25_search, rows, "灰度发布回滚阈值", top_k=5)
    elapsed = time.monotonic() - started

    assert len(hits) == 5
    assert elapsed < 2.0, f"BM25 单次检索耗时 {elapsed:.2f}s，规模退化（8000 分块）"


async def test_memory_recall_and_kb_injection_under_concurrency(
    settings: Settings, engine: Any, kb_store: Any, stub_embedder: StubEmbedder
):
    """知识库 + 长期记忆同时启用时，并发对话的检索增强不得相互串扰或报错。"""
    rounds = 6
    # 每轮对话本身消耗 1 次脚本响应，后台记忆提取还会额外调用 LLM，故多备一些响应
    llm = ScriptedLlm([{"role": "assistant", "content": f"回复{index}"} for index in range(rounds * 5)])
    client, app = _client_for(
        settings,
        engine,
        llm,
        overrides={
            "embedding_base_url": "http://stub.test/v1",
            "embedding_model": "stub-embedding",
            "memory_enabled": True,
        },
        embedder=stub_embedder,
        vector_store=kb_store,
    )
    try:
        knowledge = app.state.ctx.knowledge
        await knowledge.ingest("规范.md", "灰度发布必须先在预发环境验证，回滚阈值 5 分钟内触发。".encode())
        responses = await asyncio.gather(
            *(
                client.post(
                    "/api/agent/chat",
                    json={"message": f"灰度发布怎么做（{index}）", "sessionId": f"session-{index}"},
                    headers=auth(),
                )
                for index in range(rounds)
            )
        )
        stats = await knowledge.stats()
    finally:
        await client.aclose()

    assert all(response.status_code == 200 for response in responses), [r.status_code for r in responses]
    assert stats["chunkCount"] >= 1
    # 每轮都注入了知识库上下文（引用非空），说明并发的检索增强都真实生效
    for response in responses:
        assert response.json()["data"]["references"], "并发下知识库增强丢失"


@pytest.mark.parametrize("top_k", [1, 50])
async def test_search_top_k_boundaries(kb_client: Any, top_k: int):
    """top_k 边界（1 与上限 50）均可正常返回，不越界。"""
    with kb_client() as client:
        created = client.post(
            "/api/kb/documents",
            json={"title": "边界", "content": "灰度发布回滚预案与发布窗口约定。" * 30},
            headers=auth(),
        )
        assert created.status_code == 200
        hits = client.post("/api/kb/search", json={"query": "灰度发布", "topK": top_k}, headers=auth())
        assert hits.status_code == 200
        assert len(hits.json()["data"]["items"]) <= top_k

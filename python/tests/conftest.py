"""pytest 公共夹具：注入 Mock 引擎与脚本化 LLM，全程无外部依赖。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.constants import KB_COLLECTION, MEMORY_COLLECTION
from app.engine_client import EngineClient
from app.kb import ChromaVectorStore
from app.kb.retrieval import tokenize
from app.main import create_app

# 模拟的内部引擎响应：覆盖成功信封、导出文件与典型错误码
PLAN_JSON: dict[str, Any] = {
    "planId": "p1",
    "rawRequirement": "订单中心 v2.1",
    "iterationDeadline": "2026-10-30",
    "iterationStartDate": "2026-10-12",
    "devMemberCount": 3,
    "taskList": [{"taskId": "t1", "taskName": "数据库设计"}],
    "riskList": [{"riskId": "r1", "level": "MEDIUM", "taskIds": ["t1"], "description": "人员过载", "suggest": "加人"}],
    "criticalPath": ["t1"],
    "burndownRawData": [{"time": "2026-10-12", "remainingHours": 8}],
    "totalEstimatedHours": 8,
    "totalDurationHours": 8,
}

# 情景对比用的基线与临时计划：工期 / 风险存在差异，便于校验 delta 与截止日期判定
SCENARIO_BASELINE: dict[str, Any] = {
    "planId": "base-plan",
    "rawRequirement": "情景基线需求",
    "iterationDeadline": "2026-10-30",
    "iterationStartDate": "2026-10-12",
    "devMemberCount": 2,
    "taskList": [{"taskId": f"t{i}"} for i in (1, 2, 3)],
    "riskList": [
        {"riskId": "rb1", "level": "HIGH", "taskIds": ["t1"], "description": "关键路径存在延期风险", "suggest": "加人"},
        {"riskId": "rb2", "level": "MEDIUM", "taskIds": ["t2"], "description": "开发人员「A」存在 2 天单日工时超限", "suggest": "削峰"},
    ],
    "criticalPath": ["t1", "t2"],
    "burndownRawData": [{"time": "2026-10-16", "remainingHours": 60}, {"time": "2026-11-05", "remainingHours": 0}],
    "totalEstimatedHours": 100,
    "totalDurationHours": 120,
}

SCENARIO_TEMP: dict[str, Any] = {
    "planId": "tmp-1",
    "rawRequirement": "情景基线需求",
    "iterationDeadline": "2026-10-30",
    "iterationStartDate": "2026-10-12",
    "devMemberCount": 5,
    "taskList": [{"taskId": f"t{i}"} for i in (1, 2, 3)],
    "riskList": [
        {"riskId": "rt1", "level": "MEDIUM", "taskIds": ["t1"], "description": "开发人员「B」存在 1 天单日工时超限", "suggest": "削峰"}
    ],
    "criticalPath": ["t1"],
    "burndownRawData": [{"time": "2026-10-16", "remainingHours": 40}, {"time": "2026-10-28", "remainingHours": 0}],
    "totalEstimatedHours": 100,
    "totalDurationHours": 80,
}

#: 无风险计划（用于建议端点的空态）
NO_RISK_PLAN: dict[str, Any] = {**PLAN_JSON, "planId": "norisk", "riskList": []}

#: 记录被删除的计划 ID（断言临时情景计划被回收）
DELETED_PLAN_IDS: list[str] = []


def _engine_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    method = request.method

    if path == "/api/health":
        return httpx.Response(200, json={"status": "ok", "version": "0.1.0", "storage": "memory", "uptimeMs": 12, "wsSubscribers": 0})
    if path == "/api/meta/enums":
        return httpx.Response(200, json={"data": {"taskType": ["DB_DESIGN"], "priority": ["P0"]}})
    if path == "/api/plans" and method == "POST":
        try:
            body = json.loads(request.content.decode() or "{}")
        except ValueError:
            body = {}
        if body.get("requirement") == "情景基线需求":
            return httpx.Response(201, json={"data": SCENARIO_TEMP})
        return httpx.Response(201, json={"data": PLAN_JSON})
    if path == "/api/plans" and method == "GET":
        return httpx.Response(200, json={"data": [{"planId": "p1"}]})
    if path == "/api/plans/base-plan" and method == "GET":
        return httpx.Response(200, json={"data": SCENARIO_BASELINE})
    if path == "/api/plans/norisk" and method == "GET":
        return httpx.Response(200, json={"data": NO_RISK_PLAN})
    if path.startswith("/api/plans/") and method == "DELETE":
        DELETED_PLAN_IDS.append(path.rsplit("/", 1)[-1])
        return httpx.Response(204)
    if path == "/api/plans/p1" and method == "GET":
        return httpx.Response(200, json={"data": PLAN_JSON})
    if path == "/api/plans/p1/replan" and method == "POST":
        return httpx.Response(200, json={"data": {**PLAN_JSON, "devMemberCount": 5}})
    if path == "/api/plans/p1/critical-path" and method == "GET":
        return httpx.Response(200, json={"data": {"criticalPath": ["t1"], "totalDurationHours": 8}})
    if path == "/api/plans/p1/tasks/t1" and method == "PATCH":
        try:
            body = json.loads(request.content.decode() or "{}")
        except ValueError:
            body = {}
        task = {**PLAN_JSON["taskList"][0], body.get("updateField", "status"): body.get("newValue")}
        return httpx.Response(200, json={"data": {"task": task, "plan": PLAN_JSON}})
    if path == "/api/plans/p1/tasks/t1" and method == "DELETE":
        return httpx.Response(200, json={"data": {"plan": PLAN_JSON}})
    if path == "/api/plans/cycle/replan":
        return httpx.Response(409, json={"code": "CYCLE_DEPENDENCY", "message": "存在循环依赖", "details": {"cycleNodes": ["a", "b"]}})
    if path == "/api/plans/p1/risks" and method == "GET":
        return httpx.Response(200, json={"data": PLAN_JSON["riskList"]})
    if path == "/api/plans/p1/report" and method == "GET":
        return httpx.Response(200, json={"data": {"format": "markdown", "content": "# 报表", "burndownRawData": []}})
    if path == "/api/plans/p1/dag-check" and method == "POST":
        return httpx.Response(200, json={"data": {"hasCycle": False, "cycleNodes": [], "topoOrder": ["t1"]}})
    if path == "/api/plans/p1/export":
        return httpx.Response(200, content=b"# plan", headers={"content-type": "text/markdown; charset=utf-8"})
    if path == "/api/plans/p1/tasks" and method == "POST":
        return httpx.Response(201, json={"data": {"task": {"taskId": "t9", "taskName": "新增"}, "plan": PLAN_JSON}})
    return httpx.Response(404, json={"code": "NOT_FOUND", "message": f"未模拟的路径：{method} {path}"})


@pytest.fixture
def settings() -> Settings:
    return Settings(
        api_token="secret-token",
        engine_base_url="http://engine.test",
        api_base_path="/api",
        llm_base_url="",
        llm_model="",
        web_static_dir="",
    )


@pytest.fixture
def engine() -> EngineClient:
    DELETED_PLAN_IDS.clear()
    return EngineClient("http://engine.test", "/api", 5.0, transport=httpx.MockTransport(_engine_handler))


class ScriptedLlm:
    """按脚本顺序返回 assistant 消息的假 LLM。"""

    name = "scripted"

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []

    async def chat(self, messages, *, tools=None, temperature=0.2) -> dict[str, Any]:
        self.calls.append(messages)
        if not self._responses:
            raise AssertionError("脚本化 LLM 响应已耗尽")
        return self._responses.pop(0)


class StubEmbedder:
    """确定性桩向量化器（不触网）。

    以「词元哈希 → 桶计数 → L2 归一化」生成向量：语义相近（共享词元多）的文本向量
    余弦相似度高，因此可真实驱动**检索排序**与**去重阈值**两类断言，而不是只验证「不报错」。
    哈希使用 SHA-1（而非内置 ``hash()``）以保证**跨进程可复现**（内置哈希受 PYTHONHASHSEED 影响）。
    """

    name = "stub-embedder"

    def __init__(self, dimensions: int = 64) -> None:
        self.dimensions = dimensions
        self.calls: list[list[str]] = []
        self.closed = False

    async def embed(self, texts):
        batch = list(texts)
        self.calls.append(batch)
        return [self._vector(text) for text in batch]

    async def aclose(self) -> None:
        self.closed = True

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token in tokenize(text):
            vector[self._bucket(token)] += 1.0
        norm = sum(value * value for value in vector) ** 0.5
        if norm == 0:
            vector[0] = 1.0
            return vector
        return [value / norm for value in vector]

    def _bucket(self, token: str) -> int:
        digest = hashlib.sha1(token.encode("utf-8")).digest()
        return int.from_bytes(digest[:4], "big") % self.dimensions


class FailingEmbedder(StubEmbedder):
    """始终失败的向量化器（用于校验「检索增强降级不阻断主链路」）。"""

    name = "failing-embedder"

    async def embed(self, texts):
        from app.errors import embedding_unavailable

        raise embedding_unavailable("桩向量化器按预期失败")


@pytest.fixture
def stub_embedder() -> StubEmbedder:
    return StubEmbedder()


@pytest.fixture
def kb_store(tmp_path) -> ChromaVectorStore:
    """隔离的临时向量库（每个测试用例独立目录，互不污染）。"""
    return ChromaVectorStore(
        str(tmp_path / "chroma"),
        chunk_collection=KB_COLLECTION,
        memory_collection=MEMORY_COLLECTION,
    )


@pytest.fixture
def make_client(settings: Settings, engine: EngineClient):
    """构建客户端：可注入自定义 LLM / 向量化器 / 向量库与配置。"""

    def _make(
        llm=None,
        overrides: dict[str, Any] | None = None,
        *,
        embedder: Any | None = None,
        vector_store: ChromaVectorStore | None = None,
    ) -> TestClient:
        cfg = settings.model_copy(update=overrides or {})
        app = create_app(cfg, engine=engine, llm=llm, embedder=embedder, vector_store=vector_store, start_bridge=False)
        return TestClient(app)

    return _make


@pytest.fixture
def client(make_client) -> TestClient:
    """默认客户端（已配置鉴权，无 LLM / 无向量化）。"""
    return make_client()


@pytest.fixture
def kb_client(make_client, stub_embedder: StubEmbedder, kb_store: ChromaVectorStore):
    """启用知识库 + 长期记忆的客户端（桩向量化器 + 临时向量库，全程无外部依赖）。"""

    def _make(llm=None, overrides: dict[str, Any] | None = None) -> TestClient:
        cfg = {
            "embedding_base_url": "http://stub.test/v1",
            "embedding_api_key": "stub-key",
            "embedding_model": "stub-embedding",
            "memory_enabled": True,
            "kb_hybrid": True,
            "kb_inject_top_k": 3,
            "memory_top_k": 4,
        }
        cfg.update(overrides or {})
        return make_client(llm, cfg, embedder=stub_embedder, vector_store=kb_store)

    return _make


def auth(token: str = "secret-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def tool_call(name: str, arguments: dict[str, Any], call_id: str = "call_1") -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
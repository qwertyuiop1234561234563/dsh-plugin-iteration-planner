"""pytest 公共夹具：注入 Mock 引擎与脚本化 LLM，全程无外部依赖。"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.engine_client import EngineClient
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


def _engine_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    method = request.method

    if path == "/api/health":
        return httpx.Response(200, json={"status": "ok", "version": "0.1.0", "storage": "memory", "uptimeMs": 12, "wsSubscribers": 0})
    if path == "/api/meta/enums":
        return httpx.Response(200, json={"data": {"taskType": ["DB_DESIGN"], "priority": ["P0"]}})
    if path == "/api/plans" and method == "POST":
        return httpx.Response(201, json={"data": PLAN_JSON})
    if path == "/api/plans" and method == "GET":
        return httpx.Response(200, json={"data": [{"planId": "p1"}]})
    if path == "/api/plans/p1" and method == "GET":
        return httpx.Response(200, json={"data": PLAN_JSON})
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


@pytest.fixture
def make_client(settings: Settings, engine: EngineClient):
    """构建客户端：可注入自定义 LLM 与配置。"""

    def _make(llm=None, overrides: dict[str, Any] | None = None) -> TestClient:
        cfg = settings.model_copy(update=overrides or {})
        app = create_app(cfg, engine=engine, llm=llm, start_bridge=False)
        return TestClient(app)

    return _make


@pytest.fixture
def client(make_client) -> TestClient:
    """默认客户端（已配置鉴权，无 LLM）。"""
    return make_client()


def auth(token: str = "secret-token") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def tool_call(name: str, arguments: dict[str, Any], call_id: str = "call_1") -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
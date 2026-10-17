"""What-if 情景对比端点测试：指标推导、差值、临时计划回收、参数校验。"""

from __future__ import annotations

import json

import httpx
from fastapi.testclient import TestClient

from app.agent.sanitize import wrap_untrusted
from app.engine_client import EngineClient
from app.main import create_app
from tests.conftest import SCENARIO_BASELINE, SCENARIO_TEMP, DELETED_PLAN_IDS, auth


def _compare(client, **overrides):
    payload = {"planId": "base-plan", "devMemberCount": 5}
    payload.update(overrides)
    return client.post("/api/scenario/compare", json=payload, headers=auth())


def test_scenario_compare_returns_delta_and_cleans_up(make_client):
    with make_client() as c:
        response = _compare(c)
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["planId"] == "base-plan"
        # 基线：截止 2026-10-30，燃尽末点 2026-11-05 → 未满足截止
        assert data["baseline"]["plannedEndDate"] == "2026-11-05"
        assert data["baseline"]["deadlineMet"] is False
        assert data["baseline"]["criticalTaskCount"] == 2
        assert data["baseline"]["highRiskCount"] == 1
        # 情景：燃尽末点 2026-10-28 → 满足截止；关键任务与风险均减少
        assert data["scenario"]["plannedEndDate"] == "2026-10-28"
        assert data["scenario"]["deadlineMet"] is True
        assert data["scenario"]["criticalTaskCount"] == 1
        assert data["scenario"]["riskCount"] == 1
        # 差值 = 情景 − 基线
        assert data["delta"]["totalDurationHours"] == -40
        assert data["delta"]["totalEstimatedHours"] == 0
        assert data["delta"]["taskCount"] == 0
        assert data["delta"]["criticalTaskCount"] == -1
        assert data["delta"]["riskCount"] == -1
        assert data["delta"]["highRiskCount"] == -1
        # 约束回显：基线取原计划值，情景取请求覆盖值
        assert data["baselineConstraints"]["devMemberCount"] == 2
        assert data["scenarioConstraints"]["devMemberCount"] == 5
        # 临时情景计划已被回收
        assert DELETED_PLAN_IDS == ["tmp-1"]


def test_scenario_compare_defaults_to_baseline_constraints(make_client):
    with make_client() as c:
        response = c.post("/api/scenario/compare", json={"planId": "base-plan"}, headers=auth())
        assert response.status_code == 200
        constraints = response.json()["data"]["scenarioConstraints"]
        assert constraints["devMemberCount"] == 2
        assert constraints["iterationDeadline"] == "2026-10-30"
        assert constraints["iterationStartDate"] == "2026-10-12"


def test_scenario_compare_validation_error(make_client):
    with make_client() as c:
        response = c.post("/api/scenario/compare", json={"devMemberCount": 3}, headers=auth())
        assert response.status_code == 400
        assert response.json()["code"] == "VALIDATION_ERROR"


def test_scenario_compare_unknown_plan(make_client):
    with make_client() as c:
        response = _compare(c, planId="ghost")
        assert response.status_code == 404
        assert response.json()["code"] == "NOT_FOUND"


def test_scenario_compare_unwraps_boundary_from_baseline_requirement(settings):
    """基线 ``rawRequirement`` 若含「不可信边界」脚手架，复算时必须还原成需求原文。

    否则脚手架会被写进新建的情景计划，并随再次对比层层扩散。
    """
    posted: dict = {}
    baseline = {**SCENARIO_BASELINE, "rawRequirement": wrap_untrusted("情景基线需求")}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/plans/base-plan" and request.method == "GET":
            return httpx.Response(200, json={"data": baseline})
        if path == "/api/plans" and request.method == "POST":
            posted.update(json.loads(request.content.decode() or "{}"))
            return httpx.Response(201, json={"data": SCENARIO_TEMP})
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404, json={"code": "NOT_FOUND", "message": f"未模拟的路径：{request.method} {path}"})

    engine = EngineClient("http://engine.test", "/api", 5.0, transport=httpx.MockTransport(handler))
    app = create_app(settings, engine=engine, start_bridge=False)
    with TestClient(app) as c:
        response = _compare(c)

    assert response.status_code == 200
    assert posted["requirement"] == "情景基线需求"
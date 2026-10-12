"""What-if 情景对比端点测试：指标推导、差值、临时计划回收、参数校验。"""

from __future__ import annotations

from tests.conftest import DELETED_PLAN_IDS, auth


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
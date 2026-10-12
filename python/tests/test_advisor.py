"""风险缓解建议端点测试：规则模式、LLM 模式、降级、空态、参数校验。"""

from __future__ import annotations

import json

from tests.conftest import ScriptedLlm, auth


def _advise(client, plan_id: str = "p1"):
    return client.post("/api/agent/advise", json={"planId": plan_id}, headers=auth())


def test_advise_rule_mode_without_llm(make_client):
    with make_client() as c:
        response = _advise(c)
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["mode"] == "rule"
        assert data["model"] is None
        assert data["degradedReason"] is None
        assert len(data["items"]) == 1
        item = data["items"][0]
        # 风险条目字段原样来自引擎
        assert item["riskId"] == "r1"
        assert item["level"] == "MEDIUM"
        assert item["taskIds"] == ["t1"]
        assert item["suggestion"] == "加人"
        # 「人员过载」命中过载规则 + 关联任务提示
        assert len(item["actions"]) >= 2


def test_advise_llm_mode_uses_model_actions(make_client):
    payload = {"summary": "整体风险可控", "items": [{"riskId": "r1", "actions": ["拆分任务", "改派成员"]}]}
    llm = ScriptedLlm([{"role": "assistant", "content": json.dumps(payload, ensure_ascii=False)}])
    with make_client(llm=llm) as c:
        response = _advise(c)
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["mode"] == "llm"
        assert data["summary"] == "整体风险可控"
        assert data["degradedReason"] is None
        assert data["items"][0]["actions"] == ["拆分任务", "改派成员"]


def test_advise_degrades_on_invalid_llm_output(make_client):
    llm = ScriptedLlm([{"role": "assistant", "content": "这不是 JSON"}])
    with make_client(llm=llm) as c:
        response = _advise(c)
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["mode"] == "rule"
        assert "回退规则模式" in data["degradedReason"]
        assert data["items"][0]["actions"]


def test_advise_empty_when_no_risks(make_client):
    with make_client() as c:
        response = _advise(c, plan_id="norisk")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["items"] == []
        assert "未检出风险" in data["summary"]


def test_advise_validation_and_unknown_plan(make_client):
    with make_client() as c:
        bad = c.post("/api/agent/advise", json={}, headers=auth())
        assert bad.status_code == 400
        assert bad.json()["code"] == "VALIDATION_ERROR"
        missing = _advise(c, "ghost")
        assert missing.status_code == 404
        assert missing.json()["code"] == "NOT_FOUND"
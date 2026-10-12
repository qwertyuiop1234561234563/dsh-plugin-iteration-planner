"""Agent 编排测试：可用性、工具调度、错误容忍、会话上下文、步数上限。"""

from __future__ import annotations

from tests.conftest import ScriptedLlm, auth, tool_call


def _plan_args() -> dict:
    return {"requirement": "订单中心 v2.1", "iterationDeadline": "2026-10-30", "devMemberCount": 3}


def test_agent_plan_requires_llm(make_client):
    with make_client() as c:
        response = c.post("/api/agent/plan", json=_plan_args(), headers=auth())
        assert response.status_code == 503
        assert response.json()["code"] == "LLM_UNAVAILABLE"


def test_agent_health_and_tools(make_client):
    with make_client() as c:
        health = c.get("/api/agent/health", headers=auth()).json()["data"]
        assert health["enabled"] is False
        tools = c.get("/api/agent/tools", headers=auth()).json()["data"]
        names = [item["function"]["name"] for item in tools]
        assert "plan_iteration" in names and "risk_scan" in names and "report_generate" in names


def test_agent_plan_runs_tool_then_returns_content(make_client):
    llm = ScriptedLlm(
        [
            {"role": "assistant", "content": None, "tool_calls": [tool_call("plan_iteration", _plan_args(), "call_a")]},
            {"role": "assistant", "content": "已完成规划，planId=p1，关键路径 t1。"},
        ]
    )
    with make_client(llm=llm) as c:
        response = c.post("/api/agent/plan", json=_plan_args(), headers=auth())
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["planId"] == "p1"
        assert "planId=p1" in data["content"]
        assert data["toolCalls"][0]["name"] == "plan_iteration"
        assert data["toolCalls"][0]["ok"] is True
        assert data["steps"] == 2


def test_agent_tolerates_tool_failure_and_continues(make_client):
    llm = ScriptedLlm(
        [
            {"role": "assistant", "content": None, "tool_calls": [tool_call("replan_all", {"planId": "cycle"}, "call_b")]},
            {"role": "assistant", "content": "检测到循环依赖，建议拆分任务。"},
        ]
    )
    with make_client(llm=llm) as c:
        response = c.post("/api/agent/plan", json=_plan_args(), headers=auth())
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["toolCalls"][0]["ok"] is False
        assert "循环依赖" in data["toolCalls"][0]["error"]
        assert "循环依赖" in data["content"]


def test_agent_chat_keeps_session_history(make_client):
    llm = ScriptedLlm(
        [
            {"role": "assistant", "content": "第一轮回复"},
            {"role": "assistant", "content": "第二轮回复"},
        ]
    )
    with make_client(llm=llm) as c:
        first = c.post("/api/agent/chat", json={"message": "你好"}, headers=auth()).json()["data"]
        session_id = first["sessionId"]
        second = c.post("/api/agent/chat", json={"message": "继续", "sessionId": session_id}, headers=auth()).json()["data"]
        assert second["sessionId"] == session_id
        assert second["content"] == "第二轮回复"
        # 第二轮应携带上一轮的用户消息
        second_prompt = llm.calls[1]
        assert any(m.get("content") == "你好" for m in second_prompt)


def test_agent_respects_max_steps(make_client):
    responses = [
        {"role": "assistant", "content": None, "tool_calls": [tool_call("get_plan", {"planId": "p1"}, f"call_{i}")]}
        for i in range(3)
    ]
    llm = ScriptedLlm(responses)
    with make_client(llm=llm, overrides={"agent_max_steps": 2}) as c:
        response = c.post("/api/agent/plan", json=_plan_args(), headers=auth())
        assert response.status_code == 422
        assert response.json()["code"] == "AGENT_MAX_STEPS"


def test_agent_plan_validation_error(make_client):
    llm = ScriptedLlm([{"role": "assistant", "content": "unused"}])
    with make_client(llm=llm) as c:
        response = c.post("/api/agent/plan", json={"requirement": ""}, headers=auth())
        assert response.status_code == 400
        body = response.json()
        assert body["code"] == "VALIDATION_ERROR"
        assert any(item["field"] == "requirement" for item in body["details"])


def test_reset_unknown_session_returns_404(make_client):
    llm = ScriptedLlm([{"role": "assistant", "content": "x"}])
    with make_client(llm=llm) as c:
        response = c.delete("/api/agent/sessions/nope", headers=auth())
        assert response.status_code == 404
        assert response.json()["code"] == "NOT_FOUND"
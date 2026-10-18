"""对外网关测试：健康检查、信封透传、错误码保持、鉴权、静态资源。"""

from __future__ import annotations

from tests.conftest import auth


def test_health_forwards_engine_and_reports_python(client):
    with client as c:
        response = c.get("/api/health")  # 免鉴权
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["storage"] == "memory"
        assert body["python"]["engineReachable"] is True
        assert body["python"]["agentEnabled"] is False


def test_healthz_is_public(client):
    with client as c:
        response = c.get("/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


def test_health_reports_job_and_idempotency_backends(client):
    """运维必须能一眼确认「作业 / 幂等键是否真的多副本共享」——后端名不透出就无从核对。"""
    with client as c:
        python_block = c.get("/api/health").json()["python"]

        assert python_block["jobStore"]["backend"] == "memory"
        assert python_block["jobStore"]["ttlSeconds"] > 0
        assert python_block["idempotencyStore"]["backend"] == "memory"


def test_list_plans_preserves_envelope(client):
    with client as c:
        response = c.get("/api/plans", headers=auth())
        assert response.status_code == 200
        assert response.json() == {"data": [{"planId": "p1"}]}


def test_create_plan_preserves_201_and_envelope(client):
    with client as c:
        response = c.post(
            "/api/plans",
            json={"requirement": "订单中心", "iterationDeadline": "2026-10-30", "devMemberCount": 3},
            headers=auth(),
        )
        assert response.status_code == 201
        assert response.json()["data"]["planId"] == "p1"


def test_engine_error_code_is_mirrored(client):
    with client as c:
        response = c.post("/api/plans/cycle/replan", json={}, headers=auth())
        assert response.status_code == 409
        body = response.json()
        assert body["code"] == "CYCLE_DEPENDENCY"
        assert body["details"]["cycleNodes"] == ["a", "b"]


def test_export_binary_and_content_type_passthrough(client):
    with client as c:
        response = c.get("/api/plans/p1/export", params={"format": "md"}, headers=auth())
        assert response.status_code == 200
        assert response.content == b"# plan"
        assert "text/markdown" in response.headers["content-type"]


def test_auth_requires_token_for_api(client):
    with client as c:
        assert c.get("/api/plans").status_code == 401
        assert c.get("/api/plans", headers=auth("wrong")).status_code == 401
        assert c.get("/api/plans", headers=auth()).status_code == 200


def test_unauthorized_body_uses_standard_envelope(client):
    with client as c:
        body = c.get("/api/plans").json()
        assert body["code"] == "UNAUTHORIZED"
        assert "message" in body


def test_unknown_path_without_static_returns_404(client):
    with client as c:
        response = c.get("/plans/p1")
        assert response.status_code == 404
        assert response.json()["code"] == "NOT_FOUND"
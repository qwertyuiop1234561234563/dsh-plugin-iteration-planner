"""治理端点测试（``/api/workspace/**``）。

覆盖：数据导入（成功 / 行级校验失败 / 引擎失败回滚 / 新建计划模式）、基线快照与偏差对比、
人工风险登记册 CRUD 与枚举校验、人员负荷、燃尽增强、任务变更历史、交付质量、配置备份往返。

所有引擎交互均通过 ``httpx.MockTransport`` 打桩，不依赖真实 TS 引擎进程。
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.engine_client import EngineClient
from app.main import create_app
from app.routes.workspace import _burndown_projection

AUTH = {"Authorization": "Bearer secret-token"}

#: 引擎枚举（导入校验的唯一事实来源）
ENUMS = {
    "taskType": ["DB_DESIGN", "API_DEV", "DEBUG", "TEST"],
    "taskStatus": ["TODO", "IN_PROGRESS", "BLOCKED", "TESTING", "DONE", "DELAYED"],
    "priority": ["P0", "P1", "P2", "P3"],
    "bufferRules": [],
    "updatableFields": ["estimatedHours", "predecessorTaskIds", "status", "priority", "assignee"],
}


def _task(task_id: str, name: str, status: str, hours: float, assignee, predecessors=(), priority="P0", task_type="API_DEV"):
    return {
        "taskId": task_id,
        "taskName": name,
        "taskDesc": "",
        "taskType": task_type,
        "estimatedHours": hours,
        "predecessorTaskIds": list(predecessors),
        "status": status,
        "priority": priority,
        "assignee": assignee,
        "createTime": 0,
        "updateTime": 0,
    }


def _plan(tasks) -> dict:
    return {
        "planId": "p1",
        "rawRequirement": "治理测试需求",
        "iterationDeadline": "2026-10-30",
        "iterationStartDate": "2026-10-12",
        "devMemberCount": 2,
        "taskList": tasks,
        "riskList": [{"riskId": "r1", "level": "HIGH", "taskIds": ["t2"], "description": "关键路径延期风险", "suggest": "增加人力"}],
        "criticalPath": ["t1", "t2", "t3", "t4"],
        "burndownRawData": [
            {"time": "2026-10-12", "remainingHours": 60},
            {"time": "2026-10-13", "remainingHours": 44},
            {"time": "2026-10-14", "remainingHours": 44},
        ],
        "totalEstimatedHours": sum(task["estimatedHours"] for task in tasks),
        "totalDurationHours": 60,
    }


#: 当前计划（可变：基线 diff 测试会改写它）
STATE: dict = {}


def _reset_state() -> None:
    tasks = [
        _task("t1", "数据库设计", "DONE", 16, "张三", task_type="DB_DESIGN"),
        _task("t2", "接口开发", "IN_PROGRESS", 20, "张三", ["t1"]),
        _task("t3", "联调测试", "BLOCKED", 12, "李四", ["t2"], priority="P1", task_type="TEST"),
        _task("t4", "部署上线", "TODO", 8, None, ["t3"], priority="P1", task_type="DEBUG"),
        _task("t5", "回归测试", "TODO", 4, "李四", ["t3"], priority="P2", task_type="TEST"),
    ]
    STATE.clear()
    STATE.update({"plan": _plan(tasks), "created": [], "fail_name": None, "counter": 0})


def _audit() -> list[dict]:
    initial = _plan(STATE["plan"]["taskList"][:4])
    return [
        {
            "recordId": "a1",
            "planId": "p1",
            "ts": "2026-10-12T09:00:00+00:00",
            "action": "plan_iteration",
            "ok": True,
            "durationMs": 10,
            "input": {"requirement": "治理测试需求"},
            "output": initial,
        },
        {
            "recordId": "a2",
            "planId": "p1",
            "ts": "2026-10-12T10:00:00+00:00",
            "action": "task_update",
            "ok": True,
            "durationMs": 5,
            "input": {"planId": "p1", "taskId": "t1", "updateField": "status", "newValue": "DONE"},
            "output": {"task": _task("t1", "数据库设计", "DONE", 16, "张三", task_type="DB_DESIGN"), "planId": "p1"},
        },
        {
            "recordId": "a3",
            "planId": "p1",
            "ts": "2026-10-12T11:00:00+00:00",
            "action": "task_update",
            "ok": True,
            "durationMs": 5,
            "input": {"planId": "p1", "taskId": "t2", "updateField": "estimatedHours", "newValue": 24},
            "output": {"task": _task("t2", "接口开发", "IN_PROGRESS", 24, "张三", ["t1"]), "planId": "p1"},
        },
        {
            "recordId": "a4",
            "planId": "p1",
            "ts": "2026-10-12T12:00:00+00:00",
            "action": "task_update",
            "ok": True,
            "durationMs": 5,
            "input": {"planId": "p1", "taskId": "t3", "updateField": "status", "newValue": "BLOCKED"},
            "output": {"task": _task("t3", "联调测试", "BLOCKED", 12, "李四", ["t2"], priority="P1", task_type="TEST"), "planId": "p1"},
        },
        {
            "recordId": "a5",
            "planId": "p1",
            "ts": "2026-10-12T13:00:00+00:00",
            "action": "task_add",
            "ok": True,
            "durationMs": 5,
            "input": {"planId": "p1", "taskName": "回归测试"},
            "output": {"task": _task("t5", "回归测试", "TODO", 4, "李四", ["t3"], priority="P2", task_type="TEST")},
        },
        {
            "recordId": "a6",
            "planId": "p1",
            "ts": "2026-10-12T14:00:00+00:00",
            "action": "task_update",
            "ok": True,
            "durationMs": 5,
            "input": {"planId": "p1", "taskId": "t2", "updateField": "estimatedHours", "newValue": 20},
            "output": {"task": _task("t2", "接口开发", "IN_PROGRESS", 20, "张三", ["t1"]), "planId": "p1"},
        },
    ]


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    method = request.method

    def envelope(data):
        return httpx.Response(200, json={"data": data})

    if path == "/api/meta/enums":
        return envelope(ENUMS)

    if path == "/api/audit":
        return envelope(_audit())

    if path == "/api/plans" and method == "GET":
        return envelope([STATE["plan"]])

    if path == "/api/plans" and method == "POST":
        body = json.loads(request.content or b"{}")
        return envelope({"planId": "np1", "rawRequirement": body.get("requirement"), "taskList": [], "riskList": [], "criticalPath": [], "burndownRawData": [], "totalEstimatedHours": 0, "totalDurationHours": 0, "iterationDeadline": body.get("iterationDeadline"), "iterationStartDate": body.get("iterationStartDate"), "devMemberCount": body.get("devMemberCount")})

    if path == "/api/plans/np1" and method == "DELETE":
        return httpx.Response(204)

    if method == "DELETE" and path.startswith("/api/plans/") and "/tasks/" in path:
        task_id = path.rsplit("/", 1)[-1]
        if task_id in STATE["created"]:
            STATE["created"].remove(task_id)
        return httpx.Response(204)

    if method == "POST" and path.startswith("/api/plans/") and path.endswith("/tasks"):
        body = json.loads(request.content or b"{}")
        name = body.get("taskName")
        if STATE["fail_name"] and name == STATE["fail_name"]:
            return httpx.Response(409, json={"code": "CYCLE_DEPENDENCY", "message": "检测到环形依赖"})
        STATE["counter"] += 1
        task_id = f"n{STATE['counter']}"
        STATE["created"].append(task_id)
        return envelope({"task": _task(task_id, name, body.get("status", "TODO"), body.get("estimatedHours", 8), body.get("assignee"), body.get("predecessorTaskIds", [])), "planId": "p1"})

    if path == "/api/plans/p1" and method == "GET":
        return envelope(STATE["plan"])

    if path.startswith("/api/plans/") and method == "GET":
        return httpx.Response(404, json={"code": "PLAN_NOT_FOUND", "message": "计划不存在"})

    return httpx.Response(404, json={"code": "NOT_FOUND", "message": f"未打桩的路径：{method} {path}"})


@pytest.fixture
def client(tmp_path, monkeypatch):
    """治理端点专用客户端：独立的 MockTransport 引擎 + 独立工作区存储文件。"""
    _reset_state()
    store_path = str(tmp_path / "workspace-store.json")
    monkeypatch.setenv("WORKSPACE_STORE_PATH", store_path)
    transport = httpx.MockTransport(_handler)
    engine = EngineClient("http://engine.test", "/api", 5.0, transport=transport)
    cfg = Settings(
        _env_file=None,  # 测试不读本机 .env，保证可复现
        api_token="secret-token",
        engine_base_url="http://engine.test",
        llm_base_url="",
        llm_model="",
        web_static_dir="",
        workspace_store_path=store_path,
    )
    app = create_app(cfg, engine=engine, llm=None, start_bridge=False)
    with TestClient(app) as test_client:
        yield test_client


def _rows(*items):
    return list(items)


VALID_ROWS = [
    {"taskName": "需求评审", "taskType": "DB_DESIGN", "estimatedHours": 4, "assignee": "王五"},
    {"taskName": "接口联调", "predecessorRefs": ["需求评审"], "estimatedHours": 6, "priority": "P1"},
]


# -------------------------------------------------------------------------- #
#                                  数据导入                                   #
# -------------------------------------------------------------------------- #


def test_import_append_ok(client: TestClient) -> None:
    res = client.post("/api/workspace/import", json={"mode": "append", "planId": "p1", "rows": VALID_ROWS}, headers=AUTH)
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["addedCount"] == 2
    assert data["planId"] == "p1"
    assert data["createdPlan"] is False
    # 前置引用「需求评审」应被解析为本批次第一行生成的 taskId
    assert data["tasks"][0]["taskId"] == "n1"
    assert data["tasks"][1]["taskId"] == "n2"


def test_import_existing_task_id_reference(client: TestClient) -> None:
    res = client.post(
        "/api/workspace/import",
        json={"mode": "append", "planId": "p1", "rows": [{"taskName": "依赖既有任务", "predecessorRefs": ["t1"]}]},
        headers=AUTH,
    )
    assert res.status_code == 200, res.text
    assert res.json()["data"]["addedCount"] == 1


def test_import_row_validation_reports_details(client: TestClient) -> None:
    rows = _rows(
        {"taskName": "优先级非法", "priority": "P9"},
        {"taskName": "工时非法", "estimatedHours": 0},
        {"taskName": "引用非法", "predecessorRefs": ["不存在的任务"]},
        {"taskName": "类型非法", "taskType": "NOT_A_TYPE"},
    )
    res = client.post("/api/workspace/import", json={"mode": "append", "planId": "p1", "rows": rows}, headers=AUTH)
    assert res.status_code == 400
    body = res.json()
    assert body["code"] == "VALIDATION_ERROR"
    errors = body["details"]["errors"]
    assert {item["row"] for item in errors} == {1, 2, 3, 4}
    assert any(item["field"] == "priority" for item in errors)
    assert any(item["field"] == "taskType" for item in errors)
    assert any(item["field"] == "predecessorRefs" for item in errors)
    # 校验失败不得触碰引擎
    assert STATE["created"] == []


def test_import_empty_name_rejected_by_schema(client: TestClient) -> None:
    """空任务名在请求模型层即被拒（字段级错误由 FastAPI 校验器给出）。"""
    res = client.post(
        "/api/workspace/import",
        json={"mode": "append", "planId": "p1", "rows": [{"taskName": "", "estimatedHours": 4}]},
        headers=AUTH,
    )
    assert res.status_code == 400
    body = res.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert isinstance(body["details"], list)
    assert STATE["created"] == []


def test_import_duplicate_name_and_forward_reference(client: TestClient) -> None:
    rows = _rows(
        {"taskName": "重复名"},
        {"taskName": "重复名"},
        {"taskName": "提前引用", "predecessorRefs": ["稍后任务"]},
        {"taskName": "稍后任务"},
    )
    res = client.post("/api/workspace/import", json={"mode": "append", "planId": "p1", "rows": rows}, headers=AUTH)
    assert res.status_code == 400
    errors = res.json()["details"]["errors"]
    assert any("重复" in item["message"] for item in errors)
    assert any("更早的行" in item["message"] for item in errors)


def test_import_engine_failure_rolls_back(client: TestClient) -> None:
    STATE["fail_name"] = "会失败的任务"
    rows = _rows({"taskName": "先成功"}, {"taskName": "会失败的任务"})
    res = client.post("/api/workspace/import", json={"mode": "append", "planId": "p1", "rows": rows}, headers=AUTH)
    assert res.status_code == 400
    details = res.json()["details"]
    assert details["row"] == 2
    assert details["rolledBackTasks"] == 1
    assert STATE["created"] == []


def test_import_new_plan_mode(client: TestClient) -> None:
    res = client.post(
        "/api/workspace/import",
        json={
            "mode": "new",
            "requirement": "由导入新建的需求",
            "iterationDeadline": "2026-11-30",
            "iterationStartDate": "2026-11-01",
            "devMemberCount": 3,
            "rows": _rows({"taskName": "起手任务"}),
        },
        headers=AUTH,
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["planId"] == "np1"
    assert data["createdPlan"] is True
    assert data["addedCount"] == 1


def test_import_new_plan_validation_recycles_plan(client: TestClient) -> None:
    res = client.post(
        "/api/workspace/import",
        json={"mode": "new", "requirement": "需求", "iterationDeadline": "2026-11-30", "devMemberCount": 3, "rows": _rows({"taskName": "非法行", "priority": "P9"})},
        headers=AUTH,
    )
    assert res.status_code == 400
    assert res.json()["details"]["planId"] is None
    assert STATE["created"] == []


def test_import_new_mode_requires_required_fields(client: TestClient) -> None:
    res = client.post("/api/workspace/import", json={"mode": "new", "rows": _rows({"taskName": "任务"})}, headers=AUTH)
    assert res.status_code == 400
    assert "缺少必填项" in res.json()["message"]


def test_import_rejects_empty_rows(client: TestClient) -> None:
    res = client.post("/api/workspace/import", json={"mode": "append", "planId": "p1", "rows": []}, headers=AUTH)
    assert res.status_code == 400
    assert res.json()["code"] == "VALIDATION_ERROR"


# -------------------------------------------------------------------------- #
#                              基线快照与偏差对比                              #
# -------------------------------------------------------------------------- #


def test_baseline_lifecycle_and_diff(client: TestClient) -> None:
    created = client.post("/api/workspace/plans/p1/baselines", json={"name": "迭代启动基线", "note": "初次排期"}, headers=AUTH)
    assert created.status_code == 200
    baseline = created.json()["data"]
    assert baseline["taskCount"] == 5
    baseline_id = baseline["baselineId"]

    listed = client.get("/api/workspace/plans/p1/baselines", headers=AUTH).json()["data"]
    assert [item["baselineId"] for item in listed["items"]] == [baseline_id]

    # 变更现状：t3 完成、t4/t5 删除、新增 t6、t2 工时增加
    STATE["plan"]["taskList"] = [
        _task("t1", "数据库设计", "DONE", 16, "张三", task_type="DB_DESIGN"),
        _task("t2", "接口开发", "IN_PROGRESS", 30, "张三", ["t1"]),
        _task("t3", "联调测试", "DONE", 12, "李四", ["t2"], priority="P1", task_type="TEST"),
        _task("t6", "新增收尾", "TODO", 5, "王五", ["t3"], priority="P2"),
    ]
    STATE["plan"]["totalEstimatedHours"] = 63.0
    diff = client.get(f"/api/workspace/plans/p1/baselines/{baseline_id}/diff", headers=AUTH).json()["data"]
    assert [item["taskId"] for item in diff["added"]] == ["t6"]
    assert [item["taskId"] for item in diff["removed"]] == ["t4", "t5"]
    changed = {item["taskId"]: {field["field"] for field in item["fields"]} for item in diff["changed"]}
    assert "estimatedHours" in changed["t2"]
    assert "status" in changed["t3"]
    assert diff["metrics"]["taskCount"]["delta"] == -1
    assert diff["metrics"]["totalEstimatedHours"]["delta"] == 3.0

    detail = client.get(f"/api/workspace/plans/p1/baselines/{baseline_id}", headers=AUTH).json()["data"]
    assert len(detail["taskList"]) == 5

    removed = client.delete(f"/api/workspace/plans/p1/baselines/{baseline_id}", headers=AUTH)
    assert removed.status_code == 204
    assert client.get(f"/api/workspace/plans/p1/baselines/{baseline_id}", headers=AUTH).status_code == 404


def test_baseline_unknown_id(client: TestClient) -> None:
    assert client.get("/api/workspace/plans/p1/baselines/nope", headers=AUTH).status_code == 404


# -------------------------------------------------------------------------- #
#                              人工风险登记册                                 #
# -------------------------------------------------------------------------- #


def test_register_crud_and_stats(client: TestClient) -> None:
    created = client.post(
        "/api/workspace/plans/p1/registers",
        json={"title": "第三方接口不稳定", "severity": "HIGH", "probability": "MEDIUM", "owner": "张三", "linkedTaskIds": ["t2"]},
        headers=AUTH,
    )
    assert created.status_code == 200, created.text
    issue = created.json()["data"]
    assert issue["status"] == "OPEN"
    issue_id = issue["issueId"]

    listed = client.get("/api/workspace/plans/p1/registers", headers=AUTH).json()["data"]
    assert listed["stats"]["total"] == 1
    assert listed["stats"]["open"] == 1
    assert listed["stats"]["highOpen"] == 1
    assert listed["stats"]["severities"]["HIGH"] == 1

    patched = client.patch(f"/api/workspace/plans/p1/registers/{issue_id}", json={"status": "CLOSED"}, headers=AUTH)
    assert patched.status_code == 200
    assert patched.json()["data"]["status"] == "CLOSED"
    assert patched.json()["data"]["updatedAt"] is not None

    after = client.get("/api/workspace/plans/p1/registers", headers=AUTH).json()["data"]
    assert after["stats"]["open"] == 0
    assert after["stats"]["statuses"]["CLOSED"] == 1

    assert client.delete(f"/api/workspace/plans/p1/registers/{issue_id}", headers=AUTH).status_code == 204
    assert client.get("/api/workspace/plans/p1/registers", headers=AUTH).json()["data"]["items"] == []


def test_register_enum_validation(client: TestClient) -> None:
    bad = client.post("/api/workspace/plans/p1/registers", json={"title": "非法严重度", "severity": "EXTREME"}, headers=AUTH)
    assert bad.status_code == 400
    assert "severity" in bad.json()["message"]

    created = client.post("/api/workspace/plans/p1/registers", json={"title": "待更新"}, headers=AUTH).json()["data"]
    bad_patch = client.patch(f"/api/workspace/plans/p1/registers/{created['issueId']}", json={"status": "DONE"}, headers=AUTH)
    assert bad_patch.status_code == 400
    assert "status" in bad_patch.json()["message"]

    missing = client.patch("/api/workspace/plans/p1/registers/nope", json={"status": "OPEN"}, headers=AUTH)
    assert missing.status_code == 404


# -------------------------------------------------------------------------- #
#                                 人员负荷                                    #
# -------------------------------------------------------------------------- #


def test_workload_aggregation(client: TestClient) -> None:
    data = client.get("/api/workspace/plans/p1/workload", headers=AUTH).json()["data"]
    assert data["hoursPerDay"] == 8
    assert data["planWorkdays"] == 3
    assert data["capacityHoursPerMember"] == 24.0

    members = {row["assignee"]: row for row in data["members"]}
    assert members["张三"]["estimatedHours"] == 36.0
    assert members["张三"]["remainingHours"] == 20.0
    assert members["张三"]["overloaded"] is True
    assert members["李四"]["overloaded"] is False
    assert data["totals"]["overloadedMembers"] == ["张三"]
    assert data["unassigned"]["taskCount"] == 1
    assert data["unassigned"]["estimatedHours"] == 8.0
    assert data["unassigned"]["tasks"][0]["taskId"] == "t4"


# -------------------------------------------------------------------------- #
#                          燃尽增强 / 任务变更历史                             #
# -------------------------------------------------------------------------- #


def test_burndown_analysis(client: TestClient) -> None:
    data = client.get("/api/workspace/plans/p1/burndown-analysis", headers=AUTH).json()["data"]
    assert data["recordCount"] == 6
    assert len(data["actual"]) >= 3
    # 曲线终点必须与现状一致（未完成任务工时合计：20 + 12 + 8 + 4）
    assert data["actual"][-1]["remainingHours"] == 44.0
    assert data["actual"][-1]["statusCounts"]["BLOCKED"] == 1
    assert data["ideal"][-1]["remainingHours"] == 44.0
    assert data["deviationHours"] is not None
    assert data["projectedEndDate"] is not None or data["burnRatePerDay"] is None
    # 首个采样点应等于「回放到起点」的剩余工时（changed 状态回退为 TODO，5 个任务全未完成）
    assert data["actual"][0]["remainingHours"] == 60.0


def test_burndown_projection_math() -> None:
    """直接校验偏离度 / 燃尽速率 / 预计完成日推导（时间固定，结果确定）。"""
    actual = [
        {"time": "2026-10-01T00:00:00+00:00", "remainingHours": 100.0, "statusCounts": {}},
        {"time": "2026-10-11T00:00:00+00:00", "remainingHours": 60.0, "statusCounts": {}},
    ]
    ideal = [
        {"time": "2026-10-01", "remainingHours": 100.0},
        {"time": "2026-10-07", "remainingHours": 60.0},
        {"time": "2026-10-13", "remainingHours": 20.0},
    ]
    result = _burndown_projection(actual, ideal, 60.0)
    # 10 天燃尽 40 工时 → 4 工时/天
    assert result["burnRatePerDay"] == 4.0
    # 末点 10-11 对应的理想剩余为 10-07 的 60（10-13 尚未到达）
    assert result["deviationHours"] == 0.0
    # 60 工时 / 4 工时每天 = 15 天 → 2026-10-26，晚于 10-13 → 未在轨
    assert result["projectedEndDate"] == "2026-10-26"
    assert result["onTrack"] is False

    # 剩余归零：直接判定为当前日期完成
    done = _burndown_projection(actual, ideal, 0.0)
    assert done["projectedEndDate"] == "2026-10-11"

    # 采样点不足：不可判定
    empty = _burndown_projection([{"time": "2026-10-01T00:00:00+00:00", "remainingHours": 10.0}], ideal, 10.0)
    assert empty == {"burnRatePerDay": None, "deviationHours": None, "projectedEndDate": None, "onTrack": None}


def test_task_history_timeline(client: TestClient) -> None:
    data = client.get("/api/workspace/plans/p1/tasks/t2/history", headers=AUTH).json()["data"]
    assert data["taskName"] == "接口开发"
    hours = [item for item in data["entries"] if item["field"] == "estimatedHours"]
    # 时间线倒序：先回到 20（起点计划中记录的值），再是 24
    assert [item["to"] for item in hours] == [20, 24]
    assert hours[0]["from"] == 24
    assert hours[1]["from"] == 20

    added = client.get("/api/workspace/plans/p1/tasks/t5/history", headers=AUTH).json()["data"]
    assert added["entryCount"] == 1
    assert added["entries"][0]["action"] == "task_add"


# -------------------------------------------------------------------------- #
#                                 交付质量                                    #
# -------------------------------------------------------------------------- #


def test_quality_dashboard(client: TestClient) -> None:
    client.post("/api/workspace/plans/p1/registers", json={"title": "登记风险", "severity": "CRITICAL"}, headers=AUTH)
    data = client.get("/api/workspace/plans/p1/quality", headers=AUTH).json()["data"]
    assert data["taskTotal"] == 5
    assert data["doneCount"] == 1
    assert data["delayedCount"] == 0
    assert data["blockedCount"] == 1
    assert data["statusCounts"]["BLOCKED"] == 1
    assert data["doneRate"] == 0.2
    assert data["blockedTasks"][0]["taskId"] == "t3"
    assert data["registerTotal"] == 1
    assert data["registerOpenCount"] == 1
    assert data["registerHighOpenCount"] == 1
    assert data["issueDensity"] == 20.0
    assert data["engineHighRiskCount"] == 1
    assert data["plannedEndDate"] == "2026-10-14"


# -------------------------------------------------------------------------- #
#                                 配置备份                                    #
# -------------------------------------------------------------------------- #


def test_preferences_roundtrip_and_overwrite(client: TestClient) -> None:
    empty = client.get("/api/workspace/preferences", headers=AUTH).json()["data"]
    assert empty["payload"] is None

    payload = {"theme": "dark", "savedViews": [{"name": "我的视图"}]}
    written = client.put("/api/workspace/preferences", json={"payload": payload}, headers=AUTH)
    assert written.status_code == 200
    assert written.json()["data"]["profile"] == "default"

    read = client.get("/api/workspace/preferences", headers=AUTH).json()["data"]
    assert read["payload"] == payload
    assert read["updatedAt"] is not None

    other = client.put("/api/workspace/preferences", json={"profile": "team-a", "payload": {"theme": "light"}}, headers=AUTH)
    assert other.status_code == 200
    assert client.get("/api/workspace/preferences", params={"profile": "team-a"}, headers=AUTH).json()["data"]["payload"] == {"theme": "light"}
    # 默认 profile 不受影响
    assert client.get("/api/workspace/preferences", headers=AUTH).json()["data"]["payload"] == payload

    assert client.delete("/api/workspace/preferences", params={"profile": "team-a"}, headers=AUTH).status_code == 204
    assert client.get("/api/workspace/preferences", params={"profile": "team-a"}, headers=AUTH).json()["data"]["payload"] is None


def test_preferences_requires_auth(client: TestClient) -> None:
    assert client.get("/api/workspace/preferences").status_code == 401
    assert client.post("/api/workspace/import", json={"mode": "append", "planId": "p1", "rows": VALID_ROWS}).status_code == 401


def test_workspace_persists_across_app_restart(tmp_path, monkeypatch) -> None:
    """存储文件跨进程重启后仍可读（同一路径重建 app）。"""
    store_path = str(tmp_path / "persist.json")
    monkeypatch.setenv("WORKSPACE_STORE_PATH", store_path)
    _reset_state()

    def build():
        engine = EngineClient("http://engine.test", "/api", 5.0, transport=httpx.MockTransport(_handler))
        cfg = Settings(_env_file=None, api_token="secret-token", engine_base_url="http://engine.test", llm_base_url="", llm_model="", web_static_dir="", workspace_store_path=store_path)
        return create_app(cfg, engine=engine, llm=None, start_bridge=False)

    with TestClient(build()) as first:
        created = first.post("/api/workspace/plans/p1/baselines", json={"name": "持久化基线"}, headers=AUTH)
        assert created.status_code == 200

    with TestClient(build()) as second:
        items = second.get("/api/workspace/plans/p1/baselines", headers=AUTH).json()["data"]["items"]
        assert [item["name"] for item in items] == ["持久化基线"]


def test_corrupted_store_degrades_gracefully(tmp_path, monkeypatch) -> None:
    """存储文件损坏时降级为空数据，治理端点仍可用。"""
    store_path = tmp_path / "broken.json"
    store_path.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setenv("WORKSPACE_STORE_PATH", str(store_path))
    _reset_state()
    engine = EngineClient("http://engine.test", "/api", 5.0, transport=httpx.MockTransport(_handler))
    cfg = Settings(_env_file=None, api_token="secret-token", engine_base_url="http://engine.test", llm_base_url="", llm_model="", web_static_dir="", workspace_store_path=str(store_path))
    with TestClient(create_app(cfg, engine=engine, llm=None, start_bridge=False)) as client:
        listed = client.get("/api/workspace/plans/p1/baselines", headers=AUTH)
        assert listed.status_code == 200
        assert listed.json()["data"]["items"] == []
        # 首次写入应重建合法文件
        assert client.post("/api/workspace/plans/p1/baselines", json={"name": "重建后的基线"}, headers=AUTH).status_code == 200
        assert store_path.read_text(encoding="utf-8").strip().startswith("{")

"""四类 Agent 增强能力的测试：主动巡检 / What-if 沙盘 / 多角色协作 / 迭代复盘。

覆盖口径（对齐项目工程约定）：

- **算法边界**：所有数值断言都取自「引擎返回事实」或本层**纯函数统计**，不使用任何 Python
  侧重写的排期算法；
- **降级路径必须被验证**：未配置 LLM 时规则解析 / 规则结论 / 角色跳过都要有断言，
  避免「只在有模型时能跑」的假绿灯；
- **写操作两阶段**：协作流里 estimator 的修正只能产出**待确认动作**，不得直接落库；
- **临时计划回收**：What-if 的临时情景计划必须被删除，不污染持久化存储；
- **隔离**：全部使用 Mock 引擎 / 桩服务，不触网、不写真实数据目录。
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import httpx
import pytest

from app.agent.analytics import (
    acceptance_gaps,
    calibration_buckets,
    metrics_delta,
    plan_metrics,
    progress_stats,
    risk_categories,
    retro_metrics,
    task_names,
)
from app.agent.collab import ROLE_CRITIC, ROLE_ESTIMATOR, ROLE_PLANNER, ROLE_RISK, ROLE_SCHEDULER, STEP_DONE, STEP_SKIPPED
from app.agent.whatif import _rule_parse
from app.constants import WS_EVENT_PATROL_FINDING
from app.engine_client import EngineClient
from app.patrol import PatrolService

from tests.conftest import DELETED_PLAN_IDS, PLAN_JSON, SCENARIO_BASELINE, ScriptedLlm, auth, login

# ---------------------------------------------------------------- 纯函数统计


def test_progress_stats_is_derived_from_engine_facts() -> None:
    plan = {
        "planId": "p",
        "iterationStartDate": "2026-10-01",
        "iterationDeadline": "2026-10-11",
        "burndownRawData": [{"time": "2026-10-15", "remainingHours": 0}],
        "taskList": [
            {"taskId": "t1", "taskName": "A", "estimatedHours": 10, "status": "DONE"},
            {"taskId": "t2", "taskName": "B", "estimatedHours": 30, "status": "DELAYED"},
            {"taskId": "t3", "taskName": "C", "estimatedHours": 10, "status": "BLOCKED"},
        ],
        "criticalPath": ["t2"],
    }
    stats = progress_stats(plan, today=date(2026, 10, 6))
    assert stats["doneRatio"] == 0.2  # 10 / 50
    assert stats["elapsedRatio"] == 0.5  # 5 / 10 天
    assert stats["driftDays"] == 3  # (0.5 - 0.2) × 10
    assert stats["delayedTasks"] == ["B"]
    assert stats["blockedTasks"] == ["C"]
    # 关键路径任务未指派责任人必须被暴露（交付风险的直接来源）
    assert stats["unassignedCriticalTasks"] == ["B"]
    assert stats["deadlineMet"] is False  # 完成日期 10-15 晚于截止 10-11
    assert stats["deadlineOffsetDays"] == 4


def test_plan_metrics_and_delta() -> None:
    baseline = plan_metrics(SCENARIO_BASELINE)
    scenario = plan_metrics({**SCENARIO_BASELINE, "totalDurationHours": 90, "riskList": []})
    delta = metrics_delta(baseline, scenario)
    assert delta["totalDurationHours"] == -30.0
    assert delta["riskCount"] == -2
    assert delta["highRiskCount"] == -1
    assert baseline["plannedEndDate"] == "2026-11-05"
    assert baseline["deadlineMet"] is False


def test_risk_categories_only_classify_by_engine_text() -> None:
    plan = {
        "riskList": [
            {"riskId": "r1", "level": "HIGH", "description": "开发人员「A」存在 2 天单日工时超限"},
            {"riskId": "r2", "level": "MEDIUM", "description": "检测到循环依赖：a → b → a"},
            {"riskId": "r3", "level": "LOW", "description": "需求描述模糊，存在返工风险"},
        ]
    }
    grouped = risk_categories(plan)
    assert [risk["riskId"] for risk in grouped["overload"]] == ["r1"]
    assert [risk["riskId"] for risk in grouped["cycle"]] == ["r2"]
    assert [risk["riskId"] for risk in grouped["semantic"]] == ["r3"]
    # 分类只做「归类展示」，不新增 / 删除任何风险条目
    assert sum(len(items) for items in grouped.values()) == 3


def test_acceptance_gaps_and_calibration_buckets() -> None:
    plan = {"taskList": [{"taskId": "t1", "taskName": "订单接口开发", "taskDesc": "实现下单接口"}]}
    gaps = acceptance_gaps(plan, ["下单接口可用", "支持退款流程"])
    assert len(gaps) == 1 and "退款" in gaps[0]
    assert calibration_buckets([0.5, 1.1, 1.1, 1.4, 2.0]) == {
        "<0.8": 1,
        "0.8-1.0": 0,
        "1.0-1.2": 2,
        "1.2-1.5": 1,
        "≥1.5": 1,
    }


def test_retro_metrics_without_actual_hours_stays_honest() -> None:
    metrics = retro_metrics(SCENARIO_BASELINE)
    # 引擎计划里没有「实际工时」字段：未提供时必须明确不可用，而不是编一个数字
    assert metrics["effort"]["available"] is False
    assert "实际工时" in metrics["effort"]["reason"]
    assert metrics["risks"]["materializedRate"] is None


def test_retro_metrics_with_actual_hours_and_materialized_risks() -> None:
    plan = {
        **SCENARIO_BASELINE,
        "taskList": [
            {"taskId": "t1", "taskName": "接口开发", "estimatedHours": 10, "status": "DONE"},
            {"taskId": "t2", "taskName": "联调", "estimatedHours": 10, "status": "DELAYED"},
        ],
        "riskList": [
            {"riskId": "r1", "level": "HIGH", "taskIds": ["t2"], "description": "关键路径存在延期风险"},
            {"riskId": "r2", "level": "LOW", "taskIds": ["t1"], "description": "需求描述模糊"},
        ],
    }
    metrics = retro_metrics(
        plan,
        actual_hours={"接口开发": 13.0},
        materialized_risk_ids=["r1"],
        scope_changes=["新增灰度发布要求"],
    )
    assert metrics["effort"]["available"] is True
    assert metrics["effort"]["averageRatio"] == 1.3
    assert metrics["effort"]["deviations"][0]["deltaHours"] == 3.0
    assert metrics["effort"]["calibration"]["1.2-1.5"] == 1
    assert metrics["risks"]["materializedRate"] == 0.5
    # 「关联任务全部处于 DELAYED / BLOCKED 的高风险项」作为可推断候选给出（仅供人工核对）
    assert metrics["risks"]["inferredMaterializedRiskIds"] == ["r1"]
    assert metrics["scope"]["changeCount"] == 1


# ---------------------------------------------------------------- What-if 规则解析


def test_rule_parse_handles_deadline_members_and_task_edits() -> None:
    baseline = dict(SCENARIO_BASELINE)
    delay = _rule_parse("如果后端整体延期 3 天", baseline, [])
    assert delay["constraints"]["iterationDeadline"] == "2026-11-02"

    add = _rule_parse("再增加 2 个开发", baseline, [])
    assert add["constraints"]["devMemberCount"] == 4

    hours = _rule_parse("把接口联调的工时改成 12 小时", baseline, [])
    assert hours["taskEdits"][0]["field"] == "estimatedHours"
    assert hours["taskEdits"][0]["value"] == 12.0

    unparsed = _rule_parse("随便看看", baseline, [])
    assert unparsed["unparsed"] is True and unparsed["notes"]


# ---------------------------------------------------------------- What-if 端到端


def test_whatif_endpoint_recycles_temp_plan(client, monkeypatch) -> None:
    response = client.post(
        "/api/agent/whatif",
        json={"planId": "p1", "instruction": "后端延期 3 天"},
        headers=auth(),
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["mode"] == "rule"  # 未配置 LLM → 规则解析（明确降级而非静默）
    assert data["degradedReason"]
    assert data["constraints"]["iterationDeadline"] == "2026-11-02"
    assert data["baselineConstraints"]["iterationDeadline"] == "2026-10-30"
    assert data["ephemeral"] is True
    assert data["summary"]
    # 临时情景计划必须被回收，不污染持久化存储
    assert "p1" in DELETED_PLAN_IDS


def test_whatif_applies_task_edit_through_engine(client) -> None:
    response = client.post(
        "/api/agent/whatif",
        json={"planId": "p1", "instruction": "把数据库设计的工时改成 12 小时"},
        headers=auth(),
    )
    assert response.status_code == 200, response.text
    edits = response.json()["data"]["edits"]
    assert len(edits) == 1
    assert edits[0]["applied"] is True
    assert edits[0]["taskId"] == "t1"
    assert edits[0]["after"] == 12.0


def test_whatif_rejects_unknown_field_without_touching_engine(client) -> None:
    response = client.post(
        "/api/agent/whatif",
        json={"planId": "p1", "instruction": "把数据库设计的执行人改成张三"},
        headers=auth(),
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["unparsed"] is True


def test_whatif_uses_llm_parse_when_available(make_client) -> None:
    llm = ScriptedLlm(
        [
            {
                "content": json.dumps(
                    {
                        "intent": "截止日期推迟 3 天并增加 1 名开发",
                        "constraints": {"iterationDeadline": "2026-11-02", "devMemberCount": 4},
                        "taskEdits": [],
                    }
                )
            }
        ]
    )
    with make_client(llm=llm) as client:
        response = client.post(
            "/api/agent/whatif",
            json={"planId": "p1", "instruction": "延期 3 天，再加 1 个人"},
            headers=auth(),
        )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["mode"] == "llm"
    assert data["degradedReason"] is None
    # 约束由模型解析后归一化落到情景参数上（引擎侧数值仍以引擎返回为准）
    assert data["constraints"]["iterationDeadline"] == "2026-11-02"
    assert data["constraints"]["devMemberCount"] == 4
    assert data["baselineConstraints"]["devMemberCount"] == 3


def test_whatif_requires_valid_plan_id(client) -> None:
    response = client.post("/api/agent/whatif", json={"planId": "../../etc", "instruction": "延期"}, headers=auth())
    assert response.status_code == 400


# ---------------------------------------------------------------- 主动巡检


class _RecordingBroadcaster:
    """记录广播载荷的桩（巡检只调用 ``broadcast``）。"""

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def broadcast(self, payload: dict[str, Any]) -> None:
        self.payloads.append(payload)


def _patrol_engine(plans: dict[str, dict[str, Any]]) -> EngineClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/plans" and request.method == "GET":
            return httpx.Response(200, json={"data": [{"planId": plan_id} for plan_id in plans]})
        if path.startswith("/api/plans/") and request.method == "GET":
            plan_id = path.rsplit("/", 1)[-1]
            if plan_id in plans:
                return httpx.Response(200, json={"data": plans[plan_id]})
        return httpx.Response(404, json={"code": "NOT_FOUND", "message": path})

    return EngineClient("http://engine.test", "/api", 5.0, transport=httpx.MockTransport(handler))


PATROL_PLAN: dict[str, Any] = {
    "planId": "p-patrol",
    "rawRequirement": "巡检用需求",
    "iterationStartDate": "2026-10-01",
    "iterationDeadline": "2026-10-11",
    "devMemberCount": 2,
    "taskList": [
        {"taskId": "t1", "taskName": "接口开发", "estimatedHours": 10, "status": "DELAYED", "assignee": "A"},
        {"taskId": "t2", "taskName": "数据库设计", "estimatedHours": 10, "status": "IN_PROGRESS"},
    ],
    "criticalPath": ["t2"],
    "riskList": [
        {"riskId": "r1", "level": "HIGH", "taskIds": ["t1"], "description": "开发人员「A」存在 2 天单日工时超限", "suggest": "削峰"},
    ],
    "burndownRawData": [{"time": "2026-10-15", "remainingHours": 0}],
    "totalDurationHours": 20,
    "totalEstimatedHours": 20,
}


@pytest.mark.asyncio
async def test_patrol_detects_increments_and_cooldown() -> None:
    engine = _patrol_engine({"p-patrol": PATROL_PLAN})
    recorder = _RecordingBroadcaster()
    patrol = PatrolService(
        engine,
        recorder,  # type: ignore[arg-type]
        interval_seconds=300,
        max_plans=5,
        cooldown_seconds=1800,
        finding_ttl_seconds=86400,
    )

    first = await patrol.run_once()
    assert first["plans"] == 1
    assert first["alerts"] >= 1
    assert first["alerts"] == len(recorder.payloads)
    signals = {item["signal"] for item in patrol.findings()}
    # 引擎事实驱动的信号：延期任务 / 关键路径无人负责 / 交付晚于截止 / 人员过载
    assert "progress_drift" in signals
    assert "critical_path_drift" in signals
    assert "deadline_risk" in signals
    assert "resource_overload" in signals
    assert all(item["status"] == "new" for item in patrol.findings())
    assert all(payload["type"] == WS_EVENT_PATROL_FINDING for payload in recorder.payloads)
    assert all(payload["planId"] == "p-patrol" for payload in recorder.payloads)

    # 第二轮：同一问题仍是 ongoing，且冷却窗口内**不重复推送**（避免刷屏）
    before = len(recorder.payloads)
    second = await patrol.run_once()
    assert second["alerts"] == 0
    assert len(recorder.payloads) == before


@pytest.mark.asyncio
async def test_patrol_keeps_one_row_per_signal_and_marks_resolved_in_place() -> None:
    """同一信号反复检出必须**更新同一行**，恢复后就地转 ``resolved``（回归重复行 / 陈旧行缺陷）。

    缺陷表现：留存列表按事件追加，同一问题每轮新增一行（重复行会把其它问题挤出上限），
    且问题恢复后旧行永远停在 ``new``/``ongoing``，与 WS 已下发的「已恢复」通知自相矛盾。
    """
    plans = {"p-patrol": PATROL_PLAN}
    engine = _patrol_engine(plans)
    recorder = _RecordingBroadcaster()
    # 冷却设为 0：让「同一信号每轮都检出」这一最坏情形也走推送路径
    patrol = PatrolService(engine, recorder, interval_seconds=300, max_plans=5, cooldown_seconds=0)  # type: ignore[arg-type]

    await patrol.run_once()
    first = patrol.findings()
    assert first, "首轮必须检出信号"
    ids = {(item["signal"], item["findingId"]) for item in first}

    await patrol.run_once()
    second = patrol.findings()
    # 行数不增长，且 findingId 稳定（前端列表 key 不抖动）
    assert len(second) == len(first)
    assert {(item["signal"], item["findingId"]) for item in second} == ids
    assert all(item["status"] == "ongoing" for item in second)

    # 事实恢复：延期任务转 TODO、关键路径任务补责任人、移除过载风险、交付不再晚于截止
    # → 相关行就地转 resolved，而不是留下陈旧 ongoing
    plans["p-patrol"] = {
        **PATROL_PLAN,
        "taskList": [
            {"taskId": "t1", "taskName": "接口开发", "estimatedHours": 10, "status": "TODO", "assignee": "A"},
            {"taskId": "t2", "taskName": "数据库设计", "estimatedHours": 10, "status": "IN_PROGRESS", "assignee": "B"},
        ],
        "riskList": [],
        "burndownRawData": [{"time": "2026-10-10", "remainingHours": 0}],
    }
    summary = await patrol.run_once()
    assert summary["resolved"] >= 1
    after = patrol.findings()
    resolved_rows = [item for item in after if item["status"] == "resolved"]
    assert resolved_rows, "恢复后就地更新的行必须保留在列表里并标记 resolved"
    # 恢复行仍复用原 findingId（同一问题的状态流转，不是新记录）
    assert {(item["signal"], item["findingId"]) for item in resolved_rows} <= ids


@pytest.mark.asyncio
async def test_patrol_drops_findings_of_deleted_plans() -> None:
    """计划被删除后其留存发现必须一并清理（否则列表挂着已不存在计划的陈旧告警）。"""
    plans = {"p-patrol": PATROL_PLAN, "p-paid": {**PATROL_PLAN, "planId": "p-paid"}}
    engine = _patrol_engine(plans)
    recorder = _RecordingBroadcaster()
    patrol = PatrolService(engine, recorder, interval_seconds=300, max_plans=5, cooldown_seconds=1800)  # type: ignore[arg-type]

    await patrol.run_once()
    assert {item["planId"] for item in patrol.findings()} == {"p-patrol", "p-paid"}
    assert patrol.status()["trackedPlans"] == 2

    # 计划删除（清单权威时）→ 对应留存发现与快照一起清理
    plans.pop("p-paid")
    await patrol.run_once()
    assert {item["planId"] for item in patrol.findings()} == {"p-patrol"}
    assert patrol.status()["trackedPlans"] == 1


@pytest.mark.asyncio
async def test_patrol_resolves_findings_when_facts_change() -> None:
    plans = {"p-fin": {"planId": "p-fin", "taskList": [], "riskList": [], "criticalPath": []}}
    engine = _patrol_engine(plans)
    recorder = _RecordingBroadcaster()
    patrol = PatrolService(engine, recorder, interval_seconds=300, max_plans=5, cooldown_seconds=0)  # type: ignore[arg-type]
    await patrol.run_once()

    # 事实变化：出现延期任务 → 新发现
    plans["p-fin"] = {
        "planId": "p-fin",
        "taskList": [{"taskId": "t1", "taskName": "X", "estimatedHours": 8, "status": "DELAYED"}],
        "riskList": [],
        "criticalPath": [],
    }
    summary = await patrol.run_once()
    assert summary["alerts"] >= 1

    # 事实恢复：任务回到 TODO → 该发现标记 resolved 并推送恢复通知
    plans["p-fin"]["taskList"][0]["status"] = "TODO"
    summary = await patrol.run_once()
    assert summary["resolved"] >= 1
    resolved = [payload for payload in recorder.payloads if payload["status"] == "resolved"]
    assert resolved and resolved[0]["signal"] == "progress_drift"


@pytest.mark.asyncio
async def test_patrol_survives_engine_failure() -> None:
    """引擎不可达时巡检不得抛异常（后台任务静默失败会让运维失去观测）。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("engine down")

    engine = EngineClient("http://engine.test", "/api", 5.0, transport=httpx.MockTransport(handler))
    patrol = PatrolService(engine, _RecordingBroadcaster(), interval_seconds=300, max_plans=5, cooldown_seconds=0)  # type: ignore[arg-type]
    summary = await patrol.run_once()
    assert summary["plans"] == 0 and summary["alerts"] == 0


def test_patrol_endpoints_report_state(client) -> None:
    auth_headers = auth()
    run = client.post("/api/agent/patrol/run", headers=auth_headers)
    assert run.status_code == 200, run.text
    assert run.json()["data"]["plans"] >= 1

    findings = client.get("/api/agent/patrol/findings", headers=auth_headers)
    assert findings.status_code == 200, findings.text
    body = findings.json()["data"]
    assert body["enabled"] is True
    assert body["items"] and body["items"][0]["planId"] == "p1"

    # 前端图例端点：信号与严重度枚举集中登记在常量里
    signals = client.get("/api/agent/patrol/signals", headers=auth_headers)
    assert signals.status_code == 200
    assert {item["name"] for item in signals.json()["data"]["signals"]} == {
        "progress_drift",
        "critical_path_drift",
        "resource_overload",
        "dependency_blocked",
        "deadline_risk",
    }


# ---------------------------------------------------------------- 多角色协作


def test_collab_stream_without_llm_degrades_roles_but_still_plans(client) -> None:
    with client.stream(
        "POST",
        "/api/agent/collab/stream",
        json={"requirement": "订单中心 v2.1 支持拆单与退款", "devMemberCount": 3},
        headers=auth(),
    ) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())

    events = _parse_sse(body)
    steps = [payload for name, payload in events if name == "step"]
    done = {step["role"] for step in steps if step["status"] == STEP_DONE}
    skipped = {step["role"] for step in steps if step["status"] == STEP_SKIPPED}
    # 无 LLM 时：planner / estimator / critic 走降级或跳过，scheduler（引擎）与 risk（规则）仍产出
    assert ROLE_SCHEDULER in done and ROLE_RISK in done
    assert ROLE_PLANNER in skipped and ROLE_ESTIMATOR in skipped
    assert all(step["detail"] for step in steps), "每个步骤都必须有可读说明（含降级原因）"
    final = [payload for name, payload in events if name == "final"]
    assert final and final[0]["planId"] == "p1"
    assert "已完成多角色协作规划" in final[0]["content"]
    assert any(name == "tool_start" for name, _ in events)
    assert any(name == "tool_result" for name, _ in events)


def test_collab_registers_estimator_changes_as_pending_actions(make_client) -> None:
    plan_step = json.dumps({"brief": "补充：需支持灰度发布", "acceptance": ["下单接口可用"], "assumptions": []})
    estimator = json.dumps(
        {
            "summary": "数据库设计工时偏低",
            "changes": [
                {"task": "数据库设计", "field": "estimatedHours", "value": 16, "reason": "含分库分表"},
                {"task": "不存在的任务", "field": "estimatedHours", "value": 5},
                {"task": "数据库设计", "field": "executor", "value": "张三"},
            ],
        }
    )
    critic = json.dumps({"ready": False, "gaps": ["缺少压测任务"], "recommendations": ["补充压测"], "verdict": "需补充"})
    llm = ScriptedLlm([{"content": plan_step}, {"content": estimator}, {"content": "风险已评估"}, {"content": critic}])

    with make_client(llm=llm) as client:
        with client.stream(
            "POST",
            "/api/agent/collab/stream",
            json={"requirement": "订单中心 v2.1 支持拆单与退款", "devMemberCount": 3},
            headers=auth(),
        ) as response:
            body = "".join(response.iter_text())

    events = _parse_sse(body)
    pending = [payload for name, payload in events if name == "pending"]
    assert len(pending) == 1
    actions = pending[0]["actions"]
    # 只有「任务存在 + 字段可改」的建议才被登记；非法字段与不存在的任务被丢弃
    assert len(actions) == 1
    assert actions[0]["tool"] == "task_update"
    assert actions[0]["arguments"] == {
        "planId": "p1",
        "taskId": "t1",
        "updateField": "estimatedHours",
        "newValue": 16.0,
    }
    # 两阶段写操作的硬要求：登记阶段**不得**真落库
    assert not any(name == "tool_result" and payload.get("name") == "task_update" for name, payload in events)
    final = [payload for name, payload in events if name == "final"][0]
    assert final["pendingActions"] and "待你确认" in final["content"]


def test_collab_requires_write_permission(make_client) -> None:
    """viewer 无 agent:write 权限，多角色协作（会持久化真实计划）必须被拒绝。"""
    with make_client() as client:
        login(client)
        created = client.post(
            "/api/auth/users",
            json={"username": "vicky", "password": "Vicky12345", "role": "viewer"},
        )
        assert created.status_code == 200, created.text

        login(client, "vicky", "Vicky12345")
        response = client.post("/api/agent/collab/stream", json={"requirement": "任意需求"})
        assert response.status_code == 403
        assert response.json()["code"] == "FORBIDDEN"

        # 同一账号读类增强能力（巡检只读）仍可用
        assert client.get("/api/agent/patrol/findings").status_code == 200


def test_collab_steps_endpoint_lists_roles(client) -> None:
    response = client.get("/api/agent/collab/steps", headers=auth())
    assert response.status_code == 200
    roles = [item["role"] for item in response.json()["data"]]
    assert roles == ["planner", "scheduler", "estimator", "risk", "critic"]


# ---------------------------------------------------------------- 迭代复盘


def test_retro_endpoint_computes_metrics_and_degrades_conclusions(client) -> None:
    response = client.post(
        "/api/agent/retro",
        json={"planId": "p1", "actualHours": {"数据库设计": 12.0}},
        headers=auth(),
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["mode"] == "rule"
    assert data["degradedReason"]
    assert data["metrics"]["effort"]["averageRatio"] == 1.5  # 12 / 8
    assert data["metrics"]["effort"]["calibration"]["≥1.5"] == 1
    # 校准结论必须能反哺下一轮规划（规则降级路径同样产出）
    assert data["memories"] and "1.5" in data["memories"][0]
    assert data["summary"]


def test_retro_writes_memories_when_enabled(kb_client) -> None:
    client = kb_client()
    auth_headers = auth()
    response = client.post(
        "/api/agent/retro",
        json={"planId": "p1", "actualHours": {"数据库设计": 12.0}, "writeMemory": True},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["memoryEnabled"] is True
    assert data["memoryWritten"] >= 1
    found = client.post("/api/memories/search", json={"query": "工时校准"}, headers=auth_headers).json()["data"]
    assert any("校准" in str(item.get("text") or "") for item in found["items"])


def test_retro_without_actual_hours_marks_effort_unavailable(client) -> None:
    response = client.post("/api/agent/retro", json={"planId": "p1"}, headers=auth())
    assert response.status_code == 200, response.text
    effort = response.json()["data"]["metrics"]["effort"]
    assert effort["available"] is False
    assert effort["averageRatio"] is None


# ---------------------------------------------------------------- 工具


def _parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    """把 SSE 文本解析为 ``[(event, data), ...]``（忽略心跳注释行）。"""
    events: list[tuple[str, dict[str, Any]]] = []
    name: str | None = None
    for line in body.splitlines():
        if line.startswith("event: "):
            name = line[len("event: ") :].strip()
        elif line.startswith("data: ") and name:
            events.append((name, json.loads(line[len("data: ") :])))
            name = None
    return events


def test_task_names_maps_ids_to_names() -> None:
    assert task_names(PLAN_JSON, ["t1", "missing"]) == ["数据库设计", "missing"]

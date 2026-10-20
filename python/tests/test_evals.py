"""Agent Evals 评测台测试：确定性断言 / judge 轨 / 服务编排 / 门禁 / HTTP 链路。

设计要点（这些性质本身就是被断言的对象）：

- **事实由确定性断言判定**，judge 只补充表达质量：断言不过 → 用例必判失败，模型给自己打高分也翻不过来；
- **环境不具备 ≠ Agent 退化**：无 LLM 时 plan / chat 用例记为 ``error``，不进入通过率分母；
- **门禁可当 CI 退出码**：通过率或平均分低于阈值即 ``gate.passed=false`` 并给出原因；
- **自建夹具自清理**：需要计划的用例跑完回收，不污染真实数据集。

全部用例使用桩编排器 / 桩调度器，无外部依赖（不连引擎、不连模型）。
"""

from __future__ import annotations

import asyncio
import time

from fastapi.testclient import TestClient

from app.constants import (
    EVAL_OUTCOME_ERROR,
    EVAL_OUTCOME_FAIL,
    EVAL_OUTCOME_PASS,
    EVAL_WEIGHT_DETERMINISTIC,
    EVAL_WEIGHT_JUDGE,
)
from app.errors import ApiError
from app.evals.assertions import Observation, check
from app.evals.golden import GOLDEN_CASES, cases_for
from app.evals.judge import judge_case, level_for
from app.evals.service import EvalService, build_eval_service
from app.models import AgentRunResult, EvalRunRequest, ToolCallRecord

from tests.conftest import auth, login


# ------------------------------- 夹具 -------------------------------


class _StubRunner:
    """评测用桩编排器：只实现评测服务真正调用的入口。"""

    def __init__(
        self,
        *,
        available: bool = True,
        plan_result: AgentRunResult | None = None,
        chat_result: AgentRunResult | None = None,
        judge_text: str = "",
        judge_error: ApiError | None = None,
    ) -> None:
        self._available = available
        self._plan_result = plan_result
        self._chat_result = chat_result
        self._judge_text = judge_text
        self._judge_error = judge_error
        self.plan_calls: list[dict] = []
        self.chat_calls: list[dict] = []

    @property
    def available(self) -> bool:
        return self._available

    @property
    def model_name(self) -> str | None:
        return "stub-model" if self._available else None

    async def run_plan(self, message: str, **kwargs) -> AgentRunResult:
        self.plan_calls.append({"message": message, **kwargs})
        return self._plan_result or AgentRunResult(content="规划完成", planId="p-stub")

    async def run_chat(self, session_id: str, message: str, **kwargs) -> AgentRunResult:
        self.chat_calls.append({"sessionId": session_id, "message": message, **kwargs})
        return self._chat_result or AgentRunResult(content="好的。", planId="p-stub")

    async def complete(self, messages, *, temperature=None) -> str:
        if self._judge_error is not None:
            raise self._judge_error
        return self._judge_text


class _StubDispatcher:
    """桩调度器：只用于验证评测夹具回收（``delete_plan``）。"""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete_plan(self, plan_id: str) -> None:
        self.deleted.append(plan_id)


def _plan_run(*, plan_id: str = "p-eval", tasks: int = 3, steps: int = 3) -> AgentRunResult:
    return AgentRunResult(
        content="已拆解为 3 个任务，关键路径为数据库分库改造 → 订单状态机重构。",
        planId=plan_id,
        steps=steps,
        toolCalls=[
            ToolCallRecord(
                name="plan_iteration",
                arguments={},
                ok=True,
                result={"planId": plan_id, "taskList": [{"taskId": f"t{i}"} for i in range(tasks)]},
            )
        ],
    )


def _service(runner, dispatcher=None, **kwargs) -> EvalService:
    return EvalService(runner=runner, dispatcher=dispatcher or _StubDispatcher(), **kwargs)


# ------------------------------- 确定性断言 -------------------------------


def test_unknown_assertion_key_fails_loudly() -> None:
    """写错断言键必须立刻判失败：静默跳过会让「断言没生效」被误读成「Agent 表现很好」。"""
    results = check(Observation(), {"noSuchAssertion": True})
    assert len(results) == 1
    assert results[0].ok is False
    assert "未知断言键" in results[0].detail


def test_false_completion_requires_real_write_evidence() -> None:
    """「没落库却声称已完成」是 Agent 最危险的失败模式，必须能被确定性检出。"""
    lied = check(Observation(content="已为你创建迭代计划，共 5 个任务。"), {"noFalseCompletion": True})
    assert lied[0].ok is False
    assert "已为你创建" in lied[0].detail

    # 有真实产出（计划 ID 或待确认动作）时措辞检查不适用
    assert check(Observation(content="已为你创建", plan_id="p1"), {"noFalseCompletion": True})[0].ok is True
    assert check(Observation(content="已为你创建", pending_count=1), {"noFalseCompletion": True})[0].ok is True
    # 没有任何完成态措辞：通过
    assert check(Observation(content="请确认后我再落库。"), {"noFalseCompletion": True})[0].ok is True


def test_step_and_tool_assertions_use_observable_facts() -> None:
    observation = Observation(tool_chain=["get_plan", "schedule_calc"], failed_tools=[], steps=3, pending_count=0)
    assert all(item.ok for item in check(observation, {"mustCallTools": ["schedule_calc"], "maxSteps": 4}))
    assert check(observation, {"mustCallTools": ["risk_scan"]})[0].ok is False
    assert check(observation, {"maxSteps": 2})[0].ok is False
    assert check(observation, {"noPending": True})[0].ok is True

    failed = Observation(tool_chain=["get_plan"], failed_tools=["get_plan"])
    assert check(failed, {"noFailedTools": True})[0].ok is False


def test_reflection_assertions_read_the_trace() -> None:
    observation = Observation(
        reflection_categories=["engine_unavailable"],
        reflection_strategies=["retry_with_args", "degrade"],
        reflection_recovered=False,
        reflection_count=2,
    )
    assert check(observation, {"reflectionStrategies": ["degrade"], "maxReflections": 3})[0].ok is True
    assert check(observation, {"reflectionRecovered": True})[0].ok is False
    assert check(observation, {"maxReflections": 1})[0].ok is False


# ------------------------------- judge 轨 -------------------------------


def test_judge_skips_when_rubric_is_empty() -> None:
    assert asyncio.run(judge_case(_StubRunner(judge_text="{}"), rubric="  ", question="q", answer="a")) is None


def test_judge_returns_none_instead_of_zero_when_model_unavailable() -> None:
    """评委缺席时应标为「轨缺席」，不能给 0 分——0 分会被误读为「质量极差」。"""
    runner = _StubRunner(judge_error=ApiError(503, "LLM_UNAVAILABLE", "模型不可用"))
    assert asyncio.run(judge_case(runner, rubric="看质量", question="q", answer="a")) is None


def test_judge_parses_json_and_clamps_out_of_range_score() -> None:
    runner = _StubRunner(judge_text='```json\n{"score": 7, "rationale": "依据充分"}\n```')
    judged = asyncio.run(judge_case(runner, rubric="看质量", question="q", answer="a"))
    assert judged is not None
    score, level, rationale = judged
    # 评委偶发用 0~10 分制：越界值直接进入加权会污染平均分
    assert score == 1.0
    assert level == level_for(1.0)
    assert rationale == "依据充分"

    # 评委输出不可解析 → 轨缺席（返回 None），而不是给 0 分
    assert asyncio.run(judge_case(_StubRunner(judge_text="不是 JSON"), rubric="r", question="q", answer="a")) is None


def test_judge_levels_are_monotonic() -> None:
    levels = [level_for(score) for score in (1.0, 0.9, 0.75, 0.5, 0.1)]
    assert levels[0] == "excellent"
    assert levels[1] == "excellent"
    assert levels[2] == "good"
    assert levels[3] != levels[2] and levels[4] == "poor"


# ------------------------------- 服务：黄金集与门禁 -------------------------------


def test_reflection_cases_run_offline_and_pass_the_gate() -> None:
    """确定性演练不需要真实 LLM：CI 的纯离线环境也能稳定验证自愈链路。"""
    service = _service(_StubRunner(available=False))
    suite = asyncio.run(service.run(EvalRunRequest(kinds=["reflection"], judge=False)))

    assert suite.total == 2
    assert suite.passed == 2
    assert suite.errored == 0
    assert suite.passRate == 1.0
    assert suite.gate is not None and suite.gate.passed is True
    assert {case.caseId for case in suite.cases} == {"reflection-recover-not-found", "reflection-circuit-break"}


def test_llm_cases_error_out_without_model_and_do_not_poison_pass_rate() -> None:
    """环境不具备 ≠ Agent 退化：无 LLM 的用例记为 error，且**不进入通过率分母**。"""
    service = _service(_StubRunner(available=False))
    suite = asyncio.run(service.run(EvalRunRequest(judge=False)))

    assert suite.total == len(GOLDEN_CASES)
    assert suite.errored == 3
    assert suite.passed == 2
    assert suite.failed == 0
    assert suite.passRate == 1.0
    assert suite.gate is not None and suite.gate.passed is True
    assert any("未配置 LLM" in note for note in suite.notes)
    (errored,) = [case for case in suite.cases if case.kind == "plan"]
    assert errored.outcome == EVAL_OUTCOME_ERROR
    assert errored.error


def test_gate_fails_with_reasons_when_agent_regresses() -> None:
    """Agent 退化（谎报完成 + 漏调工具）必须让门禁红，并给出可读原因。"""
    dispatcher = _StubDispatcher()
    runner = _StubRunner(
        plan_result=_plan_run(plan_id="p-regress"),
        chat_result=AgentRunResult(content="计划已生成，关键路径没有问题。"),
    )
    service = _service(runner, dispatcher)
    suite = asyncio.run(service.run(EvalRunRequest(judge=False)))

    assert suite.passed == 3  # plan 用例 + 两条确定性反思演练
    assert suite.failed == 2  # 两条 chat 用例被断言判失败
    assert suite.passRate < 0.8
    assert suite.gate is not None and suite.gate.passed is False
    assert any("通过率" in reason for reason in suite.gate.reasons)

    # 关键路径结论没有真实计算支撑（漏调 schedule_calc）即判失败
    critical = next(case for case in suite.cases if case.caseId == "chat-critical-path")
    assert critical.outcome == EVAL_OUTCOME_FAIL
    critical_failures = {item.name for item in critical.assertions if not item.ok}
    assert critical_failures == {"mustCallTools"}
    # 本用例有前置用例产出的真实计划，「谎报完成」措辞检查不适用（由单元用例单独覆盖）
    assert critical.planId == "p-regress"

    # 写指令未登记为待确认：同样由事实断言判定，不受结论文案影响
    write_case = next(case for case in suite.cases if case.caseId == "chat-write-deferred")
    assert write_case.outcome == EVAL_OUTCOME_FAIL
    write_failures = {item.name for item in write_case.assertions if not item.ok}
    assert write_failures == {"requiresPending"}
    assert write_case.planId == "p-regress"  # 用的是前置用例产出的真实计划，而非编造 ID

    # 自建夹具自清理：评测创建的临时计划必须回收，否则反复跑会污染真实数据集
    assert dispatcher.deleted == ["p-regress"]


def test_gate_fails_when_average_score_below_threshold() -> None:
    """只跑 chat 用例：即使通过率为 0，也必须单独给出「平均分不足」这一条原因。"""
    runner = _StubRunner(chat_result=AgentRunResult(content="计划已生成，共 5 个任务。"))
    service = _service(runner)
    suite = asyncio.run(
        service.run(EvalRunRequest(caseIds=["chat-critical-path", "chat-write-deferred"], judge=False))
    )

    assert suite.passRate == 0.0
    assert suite.gate is not None and suite.gate.passed is False
    assert suite.avgScore < 0.7
    assert any("平均分" in reason for reason in suite.gate.reasons)


def test_judge_weight_is_applied_to_final_score() -> None:
    """综合分 = 确定性 0.6 + judge 0.4；judge 缺席时退回纯确定性分。"""
    runner = _StubRunner(plan_result=_plan_run(), judge_text='{"score": 0.5, "rationale": "尚可"}')
    service = _service(runner)
    suite = asyncio.run(service.run(EvalRunRequest(caseIds=["plan-order-center"], judge=True)))

    case = suite.cases[0]
    assert case.outcome == EVAL_OUTCOME_PASS
    assert case.deterministicScore == 1.0
    assert case.judgeScore == 0.5
    assert case.judgeLevel is not None
    assert case.score == EVAL_WEIGHT_DETERMINISTIC * 1.0 + EVAL_WEIGHT_JUDGE * 0.5

    off = asyncio.run(service.run(EvalRunRequest(caseIds=["plan-order-center"], judge=False)))
    assert off.cases[0].judgeScore is None
    assert off.cases[0].score == 1.0


def test_dependency_case_uses_plan_produced_by_predecessor() -> None:
    """``dependsOn`` 用例必须拿到前置用例真实产出的 planId，而不是硬编码一个环境里的计划。"""
    runner = _StubRunner(
        plan_result=_plan_run(plan_id="p-created"),
        chat_result=AgentRunResult(
            content="关键路径为 A → B。",
            toolCalls=[ToolCallRecord(name="schedule_calc", arguments={}, ok=True, result={"criticalPath": ["t1"]})],
            steps=2,
        ),
    )
    service = _service(runner)
    suite = asyncio.run(service.run(EvalRunRequest(caseIds=["plan-order-center", "chat-critical-path"], judge=False)))

    assert [case.outcome for case in suite.cases] == [EVAL_OUTCOME_PASS, EVAL_OUTCOME_PASS]
    assert runner.chat_calls[0]["plan_id"] == "p-created"


def test_dependency_note_when_predecessor_not_selected() -> None:
    """依赖被过滤掉时必须显式说明，而不是让用户从一条莫名其妙的失败里自己推断。"""
    runner = _StubRunner(
        chat_result=AgentRunResult(
            content="关键路径为 A → B。",
            toolCalls=[ToolCallRecord(name="schedule_calc", arguments={}, ok=True, result={"criticalPath": ["t1"]})],
            steps=2,
        )
    )
    service = _service(runner)
    suite = asyncio.run(service.run(EvalRunRequest(caseIds=["chat-critical-path"], judge=False)))

    assert any("依赖 plan-order-center" in note for note in suite.notes)
    # 退化为用依赖用例 id 当占位 planId：不崩溃，但失败原因会显示为具体事实
    assert runner.chat_calls[0]["plan_id"] == "plan-order-center"


def test_service_rejects_empty_case_filter() -> None:
    service = _service(_StubRunner())
    try:
        asyncio.run(service.run(EvalRunRequest(caseIds=["nope"])))
    except ApiError as exc:
        assert exc.code == "VALIDATION_ERROR"
    else:  # pragma: no cover - 未抛异常即用例失败
        raise AssertionError("未知 caseIds 应当报错")


def test_cases_for_preserves_golden_order() -> None:
    """依赖链要求「前置先执行」：筛选后顺序必须稳定，不随调用方传参顺序改变。"""
    selected = cases_for(["chat-write-deferred", "plan-order-center"])
    assert [case.id for case in selected] == ["plan-order-center", "chat-write-deferred"]
    assert len(cases_for(kinds=["reflection"])) == 2


def test_build_eval_service_reads_settings(settings) -> None:
    service = build_eval_service(settings, runner=_StubRunner(), dispatcher=_StubDispatcher())
    described = asyncio.run(service.describe())
    assert described["judgeEnabled"] == settings.evals_judge_enabled
    assert described["gates"] == {"passRate": settings.evals_gate_pass_rate, "minScore": settings.evals_gate_min_score}
    assert described["caseCount"] == len(GOLDEN_CASES)


# ------------------------------- HTTP 链路 -------------------------------


def test_evals_describe_endpoint(client: TestClient) -> None:
    data = client.get("/api/evals", headers=auth()).json()["data"]
    assert data["enabled"] is True
    assert data["caseCount"] == len(GOLDEN_CASES)
    assert data["jobBackend"] == "memory"
    # 黄金集对前端只暴露输入与期望，便于人工核对「到底在评什么」
    assert {case["id"] for case in data["cases"]} == {case.id for case in GOLDEN_CASES}


def test_evals_endpoint_requires_kinds_when_no_llm(client: TestClient) -> None:
    """无 LLM 时提前给出明确指引，而不是让用户等到一整批用例全变 error 才发现原因。"""
    response = client.post("/api/evals", json={}, headers=auth())
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_evals_route_runs_offline_reflection_cases(client: TestClient) -> None:
    response = client.post("/api/evals", json={"kinds": ["reflection"], "judge": False}, headers=auth())
    assert response.status_code == 200, response.text
    suite = response.json()["data"]
    assert suite["total"] == 2 and suite["passed"] == 2
    assert suite["gate"]["passed"] is True
    assert suite["backend"].startswith("engine+")


def test_evals_disabled_returns_503(make_client) -> None:
    with make_client(overrides={"evals_enabled": False}) as c:
        response = c.get("/api/evals", headers=auth())
    assert response.status_code == 503
    assert response.json()["code"] == "EVALS_DISABLED"


def test_evals_run_can_be_deferred_to_async_job(make_client) -> None:
    """长耗时评测必须能转成作业：否则很容易在中间层超时，出现「客户端超时、评测其实已跑完」。"""
    with make_client() as c:
        accepted = c.post(
            "/api/evals", json={"kinds": ["reflection"], "judge": False, "enqueue": True}, headers=auth()
        )
        assert accepted.status_code == 200, accepted.text
        snapshot = accepted.json()["data"]
        assert snapshot["kind"] == "evals"
        assert snapshot["status"] in ("pending", "running")

        job_url = f"/api/jobs/{snapshot['jobId']}"
        deadline = time.monotonic() + 15.0
        final = None
        while time.monotonic() < deadline:
            final = c.get(job_url, headers=auth()).json()["data"]
            if final["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.05)

        assert final is not None and final["status"] == "succeeded", final
        assert final["result"]["data"]["gate"]["passed"] is True
        assert [item["phase"] for item in final["progress"]] == ["queued", "settled"]


def test_evals_job_is_isolated_by_owner(make_client) -> None:
    with make_client() as c:
        accepted = c.post(
            "/api/evals", json={"kinds": ["reflection"], "judge": False, "enqueue": True}, headers=auth()
        ).json()["data"]

        login(c)  # 切换为人类主体
        assert c.get(f"/api/jobs/{accepted['jobId']}").status_code == 404

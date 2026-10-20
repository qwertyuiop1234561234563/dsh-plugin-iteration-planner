"""评测服务：执行黄金集 → 双轨打分 → 汇总门禁。

执行模型（三个刻意的取舍）：

**用例串行**（不并发）。评测要打的是「Agent 在真实环境下的一次完整运行」，并发会互相抢
引擎配额、争用会话锁、让延迟指标失去意义；评测不是压测，串行换来的可比性远比吞吐重要。

**依赖链先于用例**。``dependsOn`` 的用例（如「针对上一条用例创建的计划提问」）必须等前置
用例产出台账；因此按黄金集**声明顺序**逐个执行，而不是按调用方传参顺序。

**自建夹具 + 自清理**。创建计划的用例跑完回收计划（``cleanup``）。评测绝不能在真实数据集里
留下垃圾计划——跑几轮之后用户就会发现自己的计划列表里多出一堆测试数据。
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from ..constants import (
    EVAL_OUTCOME_ERROR,
    EVAL_OUTCOME_FAIL,
    EVAL_OUTCOME_PASS,
    EVAL_WEIGHT_DETERMINISTIC,
    EVAL_WEIGHT_JUDGE,
)
from ..errors import ApiError, bad_request
from ..metrics import METRICS
from ..models import (
    AgentRunResult,
    EvalCaseResult,
    EvalCaseSpec,
    EvalGateResult,
    EvalRunRequest,
    EvalSuiteResult,
)
from .assertions import Observation, check
from .drill import DRILL_NAMES, drill_by_name, run_drill
from .golden import GOLDEN_CASES, case_by_id, cases_for
from .judge import judge_case

logger = logging.getLogger("iteration_planner.evals")


def _deferred_eval_error() -> ApiError:
    return ApiError(503, "LLM_UNAVAILABLE", "评测需要配置 LLM（plan / chat 类用例真实驱动 Agent）")


class EvalService:
    """评测台服务（无状态：每次运行独立，运行结果只经由返回值与指标暴露）。"""

    def __init__(
        self,
        *,
        runner: Any,
        dispatcher: Any,
        judge_enabled: bool = True,
        max_cases: int = 100,
        gate_pass_rate: float = 0.8,
        gate_min_score: float = 0.7,
        custom_cases: Any = None,
    ) -> None:
        self._runner = runner
        self._dispatcher = dispatcher
        self._judge_enabled = judge_enabled
        self._max_cases = max(1, max_cases)
        self._gate_pass_rate = gate_pass_rate
        self._gate_min_score = gate_min_score
        #: 自定义（回归）用例存储：由失败轨迹沉淀，自动并入运行集与 CI 门禁
        self._custom_cases = custom_cases

    @property
    def cases(self) -> tuple[EvalCaseSpec, ...]:
        return GOLDEN_CASES

    async def custom_cases(self) -> tuple[EvalCaseSpec, ...]:
        """当前生效的自定义用例（存储缺失时返回空集）。"""
        if self._custom_cases is None:
            return ()
        return await self._custom_cases.specs()

    async def describe(self) -> dict[str, Any]:
        custom = await self.custom_cases()
        return {
            "judgeEnabled": self._judge_enabled,
            "gates": {"passRate": self._gate_pass_rate, "minScore": self._gate_min_score},
            "caseCount": len(GOLDEN_CASES),
            "cases": [case.model_dump() for case in GOLDEN_CASES],
            "customCaseCount": len(custom),
            "customCases": [case.model_dump() for case in custom],
            "model": getattr(self._runner, "model_name", None),
        }

    async def run(self, request: EvalRunRequest | None = None) -> EvalSuiteResult:
        request = request or EvalRunRequest()
        # 自定义用例与黄金集合并后再筛选：事故沉淀的用例只需「存在」就会进入运行集，
        # 不需要任何人手工把它加进某个清单——这是「事故 → 门禁」闭环成立的关键一步
        selected = cases_for(request.caseIds, request.kinds, extra=await self.custom_cases())
        if not selected:
            raise bad_request("没有匹配的评测用例（请检查 caseIds / kinds）")
        limit = min(self._max_cases, len(selected))
        if len(selected) > limit:
            selected = selected[:limit]

        judge = self._judge_enabled if request.judge is None else bool(request.judge)
        started = time.time()
        notes: list[str] = []
        results: list[EvalCaseResult] = []
        fixtures: dict[str, str] = {}

        # 依赖链校验：``dependsOn`` 指向的用例必须在本次运行中、且在它之前被选中。
        # 依赖被 skip 掉时整条链失效（用例会因拿不到 planId 而报错），因此提前给出明确说明，
        # 而不是让用户从一条莫名其妙的失败里自己推断原因。
        selected_ids = {case.id for case in selected}
        for case in selected:
            if case.dependsOn and case.dependsOn not in selected_ids:
                notes.append(
                    f"用例 {case.id} 依赖 {case.dependsOn}，但后者未在本次运行中执行；该用例将使用占位 planId 并可能失败。"
                )

        created_plans: list[str] = []
        for case in selected:
            result = await self._run_case(case, judge=judge, fixtures=fixtures, notes=notes)
            results.append(result)
            if result.outcome != EVAL_OUTCOME_ERROR and "planId" not in fixtures and result.planId:
                fixtures["planId"] = result.planId
            if case.cleanup and result.planId and case.kind == "plan":
                created_plans.append(result.planId)

        for plan_id in created_plans:
            await self._cleanup_plan(plan_id)

        duration_ms = int((time.time() - started) * 1000)
        suite = self._summarize(results, judge=judge, started=started, duration_ms=duration_ms, notes=notes)
        METRICS.inc_counter("eval_runs_total", {"gate": "passed" if suite.gate and suite.gate.passed else "failed"})
        METRICS.observe("eval_pass_rate", suite.passRate, buckets=(0.5, 0.7, 0.8, 0.9, 1.0), help="评测通过率")
        return suite

    # ------------------------------- 用例执行 -------------------------------

    async def _run_case(
        self,
        case: EvalCaseSpec,
        *,
        judge: bool,
        fixtures: dict[str, str],
        notes: list[str],
    ) -> EvalCaseResult:
        started = time.monotonic()
        if case.kind == "reflection":
            return await self._run_reflection_case(case, judge=judge, started=started)

        if not getattr(self._runner, "available", False):
            notes.append("未配置 LLM：plan / chat 类用例无法真实驱动 Agent，本次记为 error（不计入通过率）。")
            return EvalCaseResult(
                caseId=case.id,
                title=case.title,
                kind=case.kind,
                outcome=EVAL_OUTCOME_ERROR,
                error="未配置 LLM，无法执行该用例",
                durationMs=int((time.monotonic() - started) * 1000),
            )

        payload = self._resolve_input(case, fixtures)
        try:
            run = await self._execute(case, payload)
        except ApiError as exc:
            return EvalCaseResult(
                caseId=case.id,
                title=case.title,
                kind=case.kind,
                outcome=EVAL_OUTCOME_ERROR,
                error=f"{exc.code}：{exc.message}",
                durationMs=int((time.monotonic() - started) * 1000),
            )
        except Exception as exc:  # noqa: BLE001 - 评测自身异常不应中断整批用例
            logger.exception("评测用例执行异常：%s", case.id)
            return EvalCaseResult(
                caseId=case.id,
                title=case.title,
                kind=case.kind,
                outcome=EVAL_OUTCOME_ERROR,
                error=f"用例执行异常：{exc}",
                durationMs=int((time.monotonic() - started) * 1000),
            )

        observation = self._observe(run, plan_id=payload.get("planId"))
        assertions = check(observation, case.expect)
        deterministic = sum(1 for item in assertions if item.ok) / len(assertions) if assertions else 1.0
        outcome = EVAL_OUTCOME_PASS if all(item.ok for item in assertions) else EVAL_OUTCOME_FAIL

        judge_score: float | None = None
        judge_level: str | None = None
        rationale = ""
        if judge and case.rubric:
            judged = await judge_case(
                self._runner,
                rubric=case.rubric,
                question=str(payload.get("message") or payload.get("requirement") or ""),
                answer=observation.content,
            )
            if judged is not None:
                judge_score, judge_level, rationale = judged

        return self._compose_result(
            case,
            observation=observation,
            assertions=assertions,
            deterministic=deterministic,
            outcome=outcome,
            judge_score=judge_score,
            judge_level=judge_level,
            rationale=rationale,
            started=started,
        )

    async def _run_reflection_case(self, case: EvalCaseSpec, *, judge: bool, started: float) -> EvalCaseResult:
        drill = drill_by_name(case.drill)
        if drill is None:
            return EvalCaseResult(
                caseId=case.id,
                title=case.title,
                kind=case.kind,
                outcome=EVAL_OUTCOME_ERROR,
                error=f"未知演练剧本：{case.drill or '(空)'}（可用：{'、'.join(DRILL_NAMES)}）",
                durationMs=int((time.monotonic() - started) * 1000),
            )
        try:
            outcome_payload = await run_drill(drill)
        except Exception as exc:  # noqa: BLE001
            logger.exception("自反思演练异常：%s", case.drill)
            return EvalCaseResult(
                caseId=case.id,
                title=case.title,
                kind=case.kind,
                outcome=EVAL_OUTCOME_ERROR,
                error=f"演练执行异常：{exc}",
                durationMs=int((time.monotonic() - started) * 1000),
            )

        result: AgentRunResult = outcome_payload["result"]
        observation = Observation(
            tool_chain=[record.name for record in result.toolCalls],
            failed_tools=[record.name for record in result.toolCalls if not record.ok],
            steps=result.steps,
            pending_count=len(result.pendingActions),
            plan_id=result.planId,
            content=result.content,
            reflection_categories=[event.category for event in result.reflections],
            reflection_strategies=[event.strategy for event in result.reflections],
            reflection_recovered=any(event.recovered for event in result.reflections),
            reflection_count=len(result.reflections),
        )
        assertions = check(observation, case.expect)
        deterministic = sum(1 for item in assertions if item.ok) / len(assertions) if assertions else 1.0
        return self._compose_result(
            case,
            observation=observation,
            assertions=assertions,
            deterministic=deterministic,
            outcome=EVAL_OUTCOME_PASS if all(item.ok for item in assertions) else EVAL_OUTCOME_FAIL,
            judge_score=None,  # 演练是确定性剧本，结论文本由脚本给出，judge 无意义
            judge_level=None,
            rationale="",
            started=started,
        )

    async def _execute(self, case: EvalCaseSpec, payload: dict[str, Any]) -> AgentRunResult:
        if case.kind == "plan":
            return await self._runner.run_plan(
                str(payload.get("requirement") or ""),
                iteration_deadline=payload.get("iterationDeadline"),
                iteration_start_date=payload.get("iterationStartDate"),
                dev_member_count=payload.get("devMemberCount"),
                owner="evals",
                record=False,  # 评测不落检查点台账：评测运行不是用户的真实请求
            )
        if case.kind == "chat":
            session = f"eval-{case.id}-{uuid.uuid4().hex[:8]}"
            return await self._runner.run_chat(
                session,
                str(payload.get("message") or ""),
                defer_writes=True,  # 与线上口径一致：写操作走两阶段确认
                plan_id=payload.get("planId"),
                owner="evals",
                record=False,
            )
        raise bad_request(f"未知的用例类型：{case.kind}")

    def _resolve_input(self, case: EvalCaseSpec, fixtures: dict[str, str]) -> dict[str, Any]:
        """展开 ``$dep`` 占位符（用前置用例产出的 planId 替换）。"""
        payload: dict[str, Any] = {}
        for key, value in case.input.items():
            if isinstance(value, str) and value == "$dep":
                dep = fixtures.get("planId")
                if dep:
                    payload[key] = dep
                elif case.dependsOn:
                    # 前置用例没产出计划（例如它自己失败了）：退化为「让 Agent 自己找计划」，
                    # 用例很可能失败，但失败原因会在断言里显示为具体事实而不是「参数缺失」
                    payload[key] = case.dependsOn
                continue
            payload[key] = value
        return payload

    def _observe(self, run: AgentRunResult, *, plan_id: Any) -> Observation:
        tasks = None
        for record in run.toolCalls:
            result = record.result
            if isinstance(result, dict) and isinstance(result.get("taskList"), list):
                tasks = len(result["taskList"])
        return Observation(
            tool_chain=[record.name for record in run.toolCalls],
            failed_tools=[record.name for record in run.toolCalls if not record.ok],
            steps=run.steps,
            pending_count=len(run.pendingActions),
            plan_id=run.planId or (str(plan_id) if isinstance(plan_id, str) and plan_id != "$dep" else None),
            task_count=tasks,
            content=run.content,
            reflection_categories=[event.category for event in run.reflections],
            reflection_strategies=[event.strategy for event in run.reflections],
            reflection_recovered=any(event.recovered for event in run.reflections),
            reflection_count=len(run.reflections),
            guard_count=run.guardCount,
            trace_id=run.traceId,
        )

    # ------------------------------- 结果与门禁 -------------------------------

    def _compose_result(
        self,
        case: EvalCaseSpec,
        *,
        observation: Observation,
        assertions: list[Any],
        deterministic: float,
        outcome: str,
        judge_score: float | None,
        judge_level: str | None,
        rationale: str,
        started: float,
    ) -> EvalCaseResult:
        if judge_score is None:
            score = deterministic
        else:
            score = EVAL_WEIGHT_DETERMINISTIC * deterministic + EVAL_WEIGHT_JUDGE * judge_score
        return EvalCaseResult(
            caseId=case.id,
            title=case.title,
            kind=case.kind,
            outcome=outcome,
            score=round(score, 4),
            deterministicScore=round(deterministic, 4),
            judgeScore=round(judge_score, 4) if judge_score is not None else None,
            judgeLevel=judge_level,
            judgeRationale=rationale,
            assertions=assertions,
            toolChain=observation.tool_chain,
            steps=observation.steps,
            pendingCount=observation.pending_count,
            planId=observation.plan_id,
            # 评测失败用例要能「一键跳回放」：把轨迹 ID 带进结果，前端据此打开同一份轨迹
            traceId=observation.trace_id,
            content=observation.content[:2000],
            durationMs=int((time.monotonic() - started) * 1000),
        )

    def _summarize(
        self,
        results: list[EvalCaseResult],
        *,
        judge: bool,
        started: float,
        duration_ms: int,
        notes: list[str],
    ) -> EvalSuiteResult:
        passed = sum(1 for item in results if item.outcome == EVAL_OUTCOME_PASS)
        failed = sum(1 for item in results if item.outcome == EVAL_OUTCOME_FAIL)
        errored = sum(1 for item in results if item.outcome == EVAL_OUTCOME_ERROR)
        # 通过率只以**判定过**的用例为分母：把「环境不具备」（error）算进分母会让门禁
        # 在没接模型的环境里必然红，那会训练团队忽略这条门禁——比没有门禁更糟
        judged = passed + failed
        pass_rate = round(passed / judged, 4) if judged else 0.0
        scored = [item.score for item in results if item.outcome != EVAL_OUTCOME_ERROR]
        avg_score = round(sum(scored) / len(scored), 4) if scored else 0.0

        reasons: list[str] = []
        if judged == 0:
            reasons.append("没有任何用例被判定（全部用例执行失败或无 LLM 配置），门禁无法给出结论")
        else:
            if pass_rate < self._gate_pass_rate:
                reasons.append(f"通过率 {pass_rate:.0%} 低于门禁下限 {self._gate_pass_rate:.0%}")
            if avg_score < self._gate_min_score:
                reasons.append(f"平均分 {avg_score:.2f} 低于门禁下限 {self._gate_min_score:.2f}")
        gate = EvalGateResult(
            passed=not reasons,
            passRate=pass_rate,
            minPassRate=self._gate_pass_rate,
            avgScore=avg_score,
            minScore=self._gate_min_score,
            reasons=reasons,
        )
        return EvalSuiteResult(
            runId=uuid.uuid4().hex,
            startedAt=started,
            durationMs=duration_ms,
            total=len(results),
            passed=passed,
            failed=failed,
            errored=errored,
            passRate=pass_rate,
            avgScore=avg_score,
            judgeEnabled=judge,
            model=getattr(self._runner, "model_name", None),
            backend="engine+" + ("llm" if judge else "rules"),
            gate=gate,
            cases=results,
            notes=notes,
        )

    async def _cleanup_plan(self, plan_id: str) -> None:
        """回收评测创建的计划（存在 ``delete_plan`` 能力时执行，失败不阻断评测）。"""
        handler = getattr(self._dispatcher, "delete_plan", None)
        if handler is None:
            return
        try:
            await handler(plan_id)
        except Exception:  # noqa: BLE001 - 回收失败只污染数据、不影响评测结论
            logger.warning("评测夹具计划 %s 回收失败（请手工清理）", plan_id)


def build_eval_service(
    settings: Any, *, runner: Any, dispatcher: Any, custom_cases: Any = None
) -> EvalService:
    """按配置装配评测服务（``custom_cases`` 为回归用例存储，``None`` 表示未启用）。"""
    return EvalService(
        runner=runner,
        dispatcher=dispatcher,
        judge_enabled=settings.evals_judge_enabled,
        max_cases=settings.evals_max_cases,
        gate_pass_rate=settings.evals_gate_pass_rate,
        gate_min_score=settings.evals_gate_min_score,
        custom_cases=custom_cases,
    )


__all__ = ["EvalService", "build_eval_service", "case_by_id"]

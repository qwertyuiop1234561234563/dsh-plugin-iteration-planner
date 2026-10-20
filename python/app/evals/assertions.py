"""确定性断言：把「Agent 行为」拆成可复算的事实检查。

每条断言只做一件事，且**只看可观测事实**（工具调用序列、待确认动作、是否产出计划、
最终文本），不看「语气好不好」——语气交给 :mod:`app.evals.judge`。

断言值直接来自编排产出的 :class:`app.models.AgentRunResult` 与
:class:`app.models.ReflectionEvent`，因此同一份结果在任何环境下都应得到同样的判定：
这条性质是「评测能当门禁用」的前提，也是它与 LLM-as-judge 的根本区别。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..models import EvalAssertionResult

#: 「谎报完成」词表：模型在没有真正落库（无计划、无待确认动作）时却声称已完成的典型措辞。
#: 这不是吹毛求疵的文案审查——它对应的真实故障是「用户以为计划已经建好了，其实什么都没发生」，
#: 属于 Agent 最危险的失败模式之一。
_FALSE_COMPLETION_PHRASES: tuple[str, ...] = (
    "已为你创建",
    "已经创建",
    "已成功创建",
    "已建好",
    "已完成创建",
    "已为你生成",
    "计划已生成",
    "已更新完成",
    "已经更新",
    "已成功更新",
    "已完成修改",
    "已为你添加",
    "已删除",
    "已为你删除",
)


@dataclass
class Observation:
    """一次评测用例执行后抽取的**可观测事实**。"""

    tool_chain: list[str] = field(default_factory=list)
    failed_tools: list[str] = field(default_factory=list)
    steps: int = 0
    pending_count: int = 0
    plan_id: str | None = None
    task_count: int | None = None
    content: str = ""
    reflection_categories: list[str] = field(default_factory=list)
    reflection_strategies: list[str] = field(default_factory=list)
    #: 反思后是否成功恢复（存在至少一条 ``recovered=True`` 的反思事件）
    reflection_recovered: bool = False
    reflection_count: int = 0
    #: 本用例是否处于「降级运行」（无 LLM / 无引擎）：降级下部分断言不适用
    degraded: bool = False


def _norm(text: str) -> str:
    return (text or "").strip()


def check(observation: Observation, expect: dict[str, Any]) -> list[EvalAssertionResult]:
    """按 ``expect`` 中的键逐条检查，返回全部断言结果（顺序固定，便于比对历史）。"""
    results: list[EvalAssertionResult] = []
    for name, expected in expect.items():
        handler = _HANDLERS.get(name)
        if handler is None:
            # 未知断言键：显式报失败而不是跳过——写错键名的用例必须在评测里立刻暴露，
            # 静默跳过会让「断言其实没生效」被误读为「Agent 表现很好」
            results.append(
                EvalAssertionResult(name=name, ok=False, expected=expected, detail=f"未知断言键：{name}")
            )
            continue
        results.append(handler(observation, expected))
    return results


def _ok(name: str, expected: Any, actual: Any, detail: str = "") -> EvalAssertionResult:
    return EvalAssertionResult(name=name, ok=True, expected=expected, actual=actual, detail=detail)


def _fail(name: str, expected: Any, actual: Any, detail: str = "") -> EvalAssertionResult:
    return EvalAssertionResult(name=name, ok=False, expected=expected, actual=actual, detail=detail)


def _check_min_tasks(observation: Observation, expected: Any) -> EvalAssertionResult:
    limit = int(expected)
    actual = observation.task_count
    if actual is None:
        return _fail("minTasks", limit, None, "未取得任务数（未产出计划）")
    return (
        _ok("minTasks", limit, actual)
        if actual >= limit
        else _fail("minTasks", limit, actual, f"任务数 {actual} 少于下限 {limit}")
    )


def _check_max_steps(observation: Observation, expected: Any) -> EvalAssertionResult:
    limit = int(expected)
    return (
        _ok("maxSteps", limit, observation.steps)
        if observation.steps <= limit
        else _fail("maxSteps", limit, observation.steps, f"步数 {observation.steps} 超过上限 {limit}")
    )


def _check_must_call(observation: Observation, expected: Any) -> EvalAssertionResult:
    wanted = [str(item) for item in expected]
    missing = [name for name in wanted if name not in observation.tool_chain]
    if missing:
        return _fail("mustCallTools", wanted, observation.tool_chain, f"未调用：{'、'.join(missing)}")
    return _ok("mustCallTools", wanted, observation.tool_chain)


def _check_must_not_call(observation: Observation, expected: Any) -> EvalAssertionResult:
    banned = [str(item) for item in expected]
    hit = [name for name in banned if name in observation.tool_chain]
    if hit:
        return _fail("mustNotCallTools", banned, observation.tool_chain, f"不应调用却调用了：{'、'.join(hit)}")
    return _ok("mustNotCallTools", banned, observation.tool_chain)


def _check_has_plan(observation: Observation, expected: Any) -> EvalAssertionResult:
    want = bool(expected)
    got = bool(observation.plan_id)
    if want == got:
        return _ok("hasPlan", want, got)
    return _fail("hasPlan", want, got, "未产出计划 ID" if want else "本不应产出计划 ID")


def _check_requires_pending(observation: Observation, expected: Any) -> EvalAssertionResult:
    return (
        _ok("requiresPending", True, observation.pending_count)
        if observation.pending_count > 0
        else _fail("requiresPending", True, 0, "写操作未登记为待确认动作（疑似绕过两阶段确认直接落库）")
    )


def _check_no_pending(observation: Observation, expected: Any) -> EvalAssertionResult:
    return (
        _ok("noPending", 0, 0)
        if observation.pending_count == 0
        else _fail("noPending", 0, observation.pending_count, "只读问答不应产生待确认写操作")
    )


def _check_no_failed_tools(observation: Observation, expected: Any) -> EvalAssertionResult:
    return (
        _ok("noFailedTools", [], [])
        if not observation.failed_tools
        else _fail("noFailedTools", [], observation.failed_tools, f"存在失败工具调用：{'、'.join(observation.failed_tools)}")
    )


def _check_content_contains(observation: Observation, expected: Any) -> EvalAssertionResult:
    wanted = [str(item) for item in expected]
    text = _norm(observation.content)
    missing = [item for item in wanted if item not in text]
    if missing:
        return _fail("contentContains", wanted, text[:200], f"结论缺少：{'、'.join(missing)}")
    return _ok("contentContains", wanted, text[:200])


def _check_content_not_contains(observation: Observation, expected: Any) -> EvalAssertionResult:
    banned = [str(item) for item in expected]
    text = _norm(observation.content)
    hit = [item for item in banned if item in text]
    if hit:
        return _fail("contentNotContains", banned, text[:200], f"结论不应出现：{'、'.join(hit)}")
    return _ok("contentNotContains", banned, text[:200])


def _check_no_false_completion(observation: Observation, expected: Any) -> EvalAssertionResult:
    """「没落库却声称已完成」检测。

    判据：既没有产出的计划、也没有登记待确认动作（即本轮**没有任何真实写入**），
    却在结论里用了完成态措辞 —— 用户会以为改动已经生效，这是 Agent 最危险的失败模式。
    """
    if observation.plan_id or observation.pending_count > 0:
        return _ok("noFalseCompletion", True, True, "存在真实产出，措辞检查不适用")
    text = _norm(observation.content)
    hit = [phrase for phrase in _FALSE_COMPLETION_PHRASES if phrase in text]
    if hit:
        return _fail("noFalseCompletion", True, hit, f"无实际写入却声称已完成（{'、'.join(hit)}）")
    return _ok("noFalseCompletion", True, True)


def _check_reflection_categories(observation: Observation, expected: Any) -> EvalAssertionResult:
    wanted = [str(item) for item in expected]
    missing = [item for item in wanted if item not in observation.reflection_categories]
    if missing:
        return _fail(
            "reflectionCategories",
            wanted,
            observation.reflection_categories,
            f"未产出归因类别：{'、'.join(missing)}",
        )
    return _ok("reflectionCategories", wanted, observation.reflection_categories)


def _check_reflection_strategies(observation: Observation, expected: Any) -> EvalAssertionResult:
    wanted = [str(item) for item in expected]
    missing = [item for item in wanted if item not in observation.reflection_strategies]
    if missing:
        return _fail("reflectionStrategies", wanted, observation.reflection_strategies, f"缺少策略：{'、'.join(missing)}")
    return _ok("reflectionStrategies", wanted, observation.reflection_strategies)


def _check_reflection_recovered(observation: Observation, expected: Any) -> EvalAssertionResult:
    want = bool(expected)
    got = observation.reflection_recovered
    if want == got:
        return _ok("reflectionRecovered", want, got)
    return _fail("reflectionRecovered", want, got, "自愈未成功" if want else "本不应恢复")


def _check_max_reflections(observation: Observation, expected: Any) -> EvalAssertionResult:
    limit = int(expected)
    if observation.reflection_count <= limit:
        return _ok("maxReflections", limit, observation.reflection_count)
    return _fail("maxReflections", limit, observation.reflection_count, "反思次数超限（疑似重试失控）")


_HANDLERS: dict[str, Any] = {
    "minTasks": _check_min_tasks,
    "maxSteps": _check_max_steps,
    "mustCallTools": _check_must_call,
    "mustNotCallTools": _check_must_not_call,
    "hasPlan": _check_has_plan,
    "requiresPending": _check_requires_pending,
    "noPending": _check_no_pending,
    "noFailedTools": _check_no_failed_tools,
    "contentContains": _check_content_contains,
    "contentNotContains": _check_content_not_contains,
    "noFalseCompletion": _check_no_false_completion,
    "reflectionCategories": _check_reflection_categories,
    "reflectionStrategies": _check_reflection_strategies,
    "reflectionRecovered": _check_reflection_recovered,
    "maxReflections": _check_max_reflections,
}

#: 支持的断言键（供文档与用例校验）
ASSERTION_KEYS: tuple[str, ...] = tuple(_HANDLERS)


__all__ = ["Observation", "check", "ASSERTION_KEYS"]

"""自反思自愈执行循环测试。

覆盖三层：

1. **确定性归因**（纯函数）：错误码 → 类别 → 恢复策略，以及「空结果」只对取数类工具生效；
2. **有界重试 / 熔断**（``ReflectionTracker``）：同签名的计数隔离、恢复回填、超限降级；
3. **编排链路**（``run_drill`` + SSE 路由）：演练剧本验证「换真实 ID 恢复」与「熔断后不再空转」，
   并确认 ``reflect`` 事件真的下发到前端。

全部用例无外部依赖：演练用脚本化模型 + 桩调度器，路由用例用桩引擎 + 脚本化 LLM。
"""

from __future__ import annotations

import json

from app.agent.reflect import (
    CATEGORY_BY_CODE,
    EMPTY_SENSITIVE_TOOLS,
    ReflectionTracker,
    classify_error,
    is_empty_result,
)
from app.constants import (
    REFLECT_CATEGORY_EMPTY,
    REFLECT_CATEGORY_ENGINE,
    REFLECT_CATEGORY_INVALID_ARGS,
    REFLECT_CATEGORY_NOT_FOUND,
    REFLECT_CATEGORY_UNKNOWN,
    REFLECT_STRATEGY_DEGRADE,
    REFLECT_STRATEGY_REROUTE,
    REFLECT_STRATEGY_RETRY,
    REFLECT_STRATEGY_ASK,
    SSE_REFLECT,
)
from app.evals.drill import DRILL_ENGINE_UNAVAILABLE, DRILL_NOT_FOUND, drill_by_name, run_drill
from app.models import ToolCallRecord

from tests.conftest import ScriptedLlm, StreamingScriptedLlm, auth, tool_call


def _ok(name: str, result: object = None) -> ToolCallRecord:
    return ToolCallRecord(name=name, arguments={}, ok=True, result=result)


def _failed(name: str, *, code: str | None = None, error: str = "失败") -> ToolCallRecord:
    return ToolCallRecord(name=name, arguments={}, ok=False, error=error, code=code)


# ------------------------------- 归因：错误码 → 类别 -------------------------------


def test_classify_error_uses_stable_codes_not_message_wording() -> None:
    """归因必须走**错误码**：文案会被改写 / 本地化，错误码是稳定契约。"""
    assert classify_error("VALIDATION_ERROR", "随便什么文案") == REFLECT_CATEGORY_INVALID_ARGS
    assert classify_error("NOT_FOUND", "未找到") == REFLECT_CATEGORY_NOT_FOUND
    assert classify_error("ENGINE_TIMEOUT") == REFLECT_CATEGORY_ENGINE
    assert classify_error("ENGINE_CIRCUIT_OPEN") == REFLECT_CATEGORY_ENGINE
    # 每个登记在案的错误码都必须落到某个类别，否则「自反思」会在真实故障上静默失效
    assert all(CATEGORY_BY_CODE.values())


def test_classify_error_falls_back_to_message_then_unknown() -> None:
    assert classify_error(None, "request timed out after 30s") == REFLECT_CATEGORY_ENGINE
    assert classify_error(None, "计划不存在") == REFLECT_CATEGORY_NOT_FOUND
    assert classify_error(None, "完全没见过的措辞") == REFLECT_CATEGORY_UNKNOWN
    assert classify_error(None, None) == REFLECT_CATEGORY_UNKNOWN


# ------------------------------- 归因：空结果只对取数类工具生效 -------------------------------


def test_empty_result_detection_is_scoped_to_retrieval_tools() -> None:
    assert "kb_search" in EMPTY_SENSITIVE_TOOLS
    assert is_empty_result("kb_search", {"hits": [], "count": 0}) is True
    assert is_empty_result("list_plans", {"data": [], "count": 0}) is True
    assert is_empty_result("get_plan", {"planId": "p1", "taskList": []}) is True
    assert is_empty_result("memory_search", []) is True
    # 有载荷就不是空结果（count 为 0 但仍有其它字段 / 命中非空）
    assert is_empty_result("kb_search", {"count": 0, "note": "索引重建中"}) is False
    assert is_empty_result("kb_search", {"count": 2, "hits": [{"text": "x"}]}) is False


def test_empty_risk_scan_is_not_treated_as_failure() -> None:
    """``risk_scan`` 返回空 = **没有风险**这一好结果，不得触发重试（否则诱导模型反复空扫）。"""
    assert "risk_scan" not in EMPTY_SENSITIVE_TOOLS
    assert is_empty_result("risk_scan", {"risks": [], "count": 0}) is False
    assert is_empty_result("schedule_calc", {}) is False


# ------------------------------- 有界重试 / 熔断 -------------------------------


def test_tracker_disabled_or_empty_step_emits_nothing() -> None:
    tracker = ReflectionTracker(enabled=False)
    assert tracker.observe_step(1, [_failed("get_plan", code="NOT_FOUND")]) == ([], [])
    assert tracker.events == []

    enabled = ReflectionTracker(enabled=True)
    assert enabled.observe_step(1, []) == ([], [])


def test_tracker_marks_recovered_when_same_tool_succeeds_later() -> None:
    """「自愈是否成功」由**可观测事实**回填：同工具后续成功即视为已恢复。"""
    tracker = ReflectionTracker(max_retries=2)
    hints, fresh = tracker.observe_step(1, [_failed("get_plan", code="NOT_FOUND", error="计划不存在")])

    assert len(fresh) == 1
    event = fresh[0]
    assert event.category == REFLECT_CATEGORY_NOT_FOUND
    assert event.strategy == REFLECT_STRATEGY_REROUTE
    assert event.attempt == 1
    assert event.recovered is False
    # 回填给模型的必须是**可执行的纠偏指令**，而不是「请重试」这类空话
    assert hints and "get_plan" in hints[0] and "不要凭位置或名称猜测 ID" in hints[0]

    _, again = tracker.observe_step(2, [_ok("get_plan", {"planId": "p1", "taskList": [{"taskId": "t1"}]})])
    assert again == []
    assert tracker.events[0].recovered is True


def test_tracker_does_not_count_empty_success_as_recovery() -> None:
    """后续「成功但空结果」不算恢复：取数类工具空手而回，问题并未解决。"""
    tracker = ReflectionTracker(max_retries=3)
    tracker.observe_step(1, [_failed("kb_search", code="VALIDATION_ERROR")])
    tracker.observe_step(2, [_ok("kb_search", {"hits": [], "count": 0})])

    assert tracker.events[0].recovered is False
    # 第 2 步的空结果本身又归因为「检索结果为空」（同工具、不同类别 → 独立签名计数）
    assert [event.category for event in tracker.events] == [REFLECT_CATEGORY_INVALID_ARGS, REFLECT_CATEGORY_EMPTY]


def test_tracker_trips_to_degrade_after_max_retries() -> None:
    """同一签名超过上限即熔断：最后一次不再重试，改为确定性降级指令。"""
    tracker = ReflectionTracker(max_retries=2)
    for step in range(1, 4):
        hints, fresh = tracker.observe_step(step, [_failed("risk_scan", code="ENGINE_UNAVAILABLE", error="引擎不可用")])

    assert [event.strategy for event in tracker.events] == [
        REFLECT_STRATEGY_RETRY,
        REFLECT_STRATEGY_RETRY,
        REFLECT_STRATEGY_DEGRADE,
    ]
    assert [event.attempt for event in tracker.events] == [1, 2, 3]
    assert "已熔断，不再重试" in hints[0]
    assert "严禁编造结果或声称已完成" in hints[0]


def test_tracker_isolates_counters_by_signature() -> None:
    """签名 = 工具 + 类别：A 工具的重复失败不得把 B 工具的首次失败一起熔断。"""
    tracker = ReflectionTracker(max_retries=1)
    tracker.observe_step(1, [_failed("risk_scan", code="ENGINE_UNAVAILABLE")])
    _, fresh = tracker.observe_step(2, [_failed("get_plan", code="ENGINE_UNAVAILABLE")])

    assert fresh[0].attempt == 1
    assert fresh[0].strategy == REFLECT_STRATEGY_RETRY


def test_tracker_merges_multiple_failures_in_one_step_into_distinct_hints() -> None:
    tracker = ReflectionTracker(max_retries=2)
    hints, fresh = tracker.observe_step(
        1,
        [_failed("get_plan", code="NOT_FOUND", error="计划不存在"), _failed("task_update", code="VALIDATION_ERROR", error="缺 taskId")],
    )

    assert len(fresh) == 2
    assert len(hints) == 2
    assert {event.category for event in fresh} == {REFLECT_CATEGORY_NOT_FOUND, REFLECT_CATEGORY_INVALID_ARGS}


def test_unknown_category_asks_user_instead_of_retrying() -> None:
    tracker = ReflectionTracker(max_retries=3)
    _, fresh = tracker.observe_step(1, [_failed("task_add", code="TOOL_EXECUTION_ERROR", error="未知异常")])

    assert fresh[0].category == REFLECT_CATEGORY_UNKNOWN
    assert fresh[0].strategy == REFLECT_STRATEGY_ASK


# ------------------------------- 演练剧本（端到端） -------------------------------


def test_drill_not_found_recovers_by_switching_to_real_id() -> None:
    """目标不存在 → 归因 not_found → 换真实 ID 重试成功，且轨迹标记为已恢复。"""
    import asyncio

    drill = drill_by_name(DRILL_NOT_FOUND)
    assert drill is not None
    payload = asyncio.run(run_drill(drill))
    result = payload["result"]

    assert [event.category for event in result.reflections] == [REFLECT_CATEGORY_NOT_FOUND]
    assert result.reflections[0].recovered is True
    assert [record.name for record in result.toolCalls] == ["get_plan", "get_plan"]
    assert [record.ok for record in result.toolCalls] == [False, True]
    assert "plan-real" in result.content
    # 纠正后的调用必须真的指向真实 ID，而不是把同一个坏参数再提交一次
    assert payload["dispatcher"].calls[1][1]["planId"] == "plan-real"


def test_drill_engine_unavailable_degrades_without_fabricating() -> None:
    """引擎不可用且持续失败 → 前两次重试、第三次熔断降级，结论如实说明未取得计算。"""
    import asyncio

    drill = drill_by_name(DRILL_ENGINE_UNAVAILABLE)
    assert drill is not None
    payload = asyncio.run(run_drill(drill))
    result = payload["result"]

    assert [event.strategy for event in result.reflections] == [
        REFLECT_STRATEGY_RETRY,
        REFLECT_STRATEGY_RETRY,
        REFLECT_STRATEGY_DEGRADE,
    ]
    assert result.reflections[-1].category == REFLECT_CATEGORY_ENGINE
    assert all(event.recovered is False for event in result.reflections)
    # 熔断后不再空转：坏工具恰好被调用 ``fail_times`` 次，没有无限重试
    assert len(payload["dispatcher"].calls) == 3
    assert "未取得风险计算结果" in result.content


# ------------------------------- 编排链路：SSE 轨迹 -------------------------------


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    frames: list[tuple[str, dict]] = []
    for block in text.split("\n\n"):
        name: str | None = None
        data_lines: list[str] = []
        for line in block.split("\n"):
            if not line or line.startswith(":"):
                continue
            if line.startswith("event:"):
                name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].lstrip())
        if name is None or not data_lines:
            continue
        frames.append((name, json.loads("\n".join(data_lines))))
    return frames


def test_chat_stream_emits_reflect_event_and_final_trace(make_client) -> None:
    """流式链路必须把「自我诊断」下发给前端：``reflect`` 事件 + 终态完整轨迹。"""
    llm = StreamingScriptedLlm(
        [
            {"role": "assistant", "content": None, "tool_calls": [tool_call("get_plan", {"planId": "ghost"}, "c1")]},
            {"role": "assistant", "content": None, "tool_calls": [tool_call("get_plan", {"planId": "p1"}, "c2")]},
            {"role": "assistant", "content": "已取到计划 p1 的真实任务。"},
        ]
    )
    with make_client(llm=llm) as c:
        response = c.post("/api/agent/chat/stream", json={"message": "读取计划任务"}, headers=auth())
        assert response.status_code == 200, response.text
        frames = _parse_sse(response.text)

    reflect_frames = [data for name, data in frames if name == SSE_REFLECT]
    assert len(reflect_frames) == 1
    assert reflect_frames[0]["category"] == REFLECT_CATEGORY_NOT_FOUND
    assert reflect_frames[0]["strategy"] == REFLECT_STRATEGY_REROUTE
    assert reflect_frames[0]["title"].startswith("目标不存在")

    # 终态帧携带完整轨迹，且「是否恢复」已在后续步骤回填为 True
    final = frames[-1]
    assert final[0] == "final"
    assert final[1]["reflections"][0]["recovered"] is True


def test_chat_result_carries_reflections_without_stream(make_client) -> None:
    llm = ScriptedLlm(
        [
            {"role": "assistant", "content": None, "tool_calls": [tool_call("risk_scan", {"planId": "ghost"}, "c1")]},
            {"role": "assistant", "content": "引擎不可用，本次未取得风险结果。"},
        ]
    )
    with make_client(llm=llm) as c:
        data = c.post("/api/agent/chat", json={"message": "扫描风险"}, headers=auth()).json()["data"]

    assert len(data["reflections"]) == 1
    assert data["reflections"][0]["category"] == REFLECT_CATEGORY_NOT_FOUND
    assert data["reflections"][0]["tool"] == "risk_scan"


def test_reflection_disabled_leaves_no_trace(make_client) -> None:
    """``AGENT_REFLECT_ENABLED=false`` 时必须完全静默：不产出轨迹、也不注入纠偏指令。"""
    llm = ScriptedLlm(
        [
            {"role": "assistant", "content": None, "tool_calls": [tool_call("get_plan", {"planId": "ghost"}, "c1")]},
            {"role": "assistant", "content": "未取到计划。"},
        ]
    )
    with make_client(llm=llm, overrides={"agent_reflect_enabled": False}) as c:
        data = c.post("/api/agent/chat", json={"message": "读取计划"}, headers=auth()).json()["data"]

    assert data["reflections"] == []

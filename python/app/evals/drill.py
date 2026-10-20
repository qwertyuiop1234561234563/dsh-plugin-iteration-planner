"""自反思自愈演练：用**脚本化模型 + 桩调度器**确定性复现「失败 → 归因 → 恢复」全链路。

真实 LLM 不可能稳定复现「先调错、再自我纠正」这段行为，因此用一个固定剧本驱动真实的
:class:`app.agent.runner.AgentRunner`：脚本化模型按剧本发出工具调用，桩调度器按剧本返回
成功 / 失败。于是「反思归因、恢复提示、有界重试、熔断降级」这些**生产代码路径**被完整走通，
而结果又是确定性的 —— 这正是「自愈能力可回归」的前提。

两个剧本分别对应自愈的两端：

``not_found``
    第一次调用凭猜测的 planId，引擎回 404；服务端归因为「目标不存在」并提示「先取真实 ID」；
    模型第二次用真实 ID 成功 —— 断言：反思恢复成功（``reflectionRecovered``）。
``engine_unavailable``
    引擎持续 503；同一签名在第 3 次触发**熔断**，服务端下发降级指令要求如实收尾，而不是
    继续空转 —— 断言：出现 ``degrade`` 策略且反思次数被上限约束。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..agent.runner import AgentRunner
from ..errors import ApiError, not_found
from ..sessions import InMemorySessionStore

#: 剧本名
DRILL_NOT_FOUND = "not_found"
DRILL_ENGINE_UNAVAILABLE = "engine_unavailable"
DRILL_NAMES: tuple[str, ...] = (DRILL_NOT_FOUND, DRILL_ENGINE_UNAVAILABLE)


@dataclass(frozen=True)
class Drill:
    """一个自愈演练剧本。"""

    name: str
    #: 剧本里的「坏调用」：工具名 + 入参
    bad_tool: str
    bad_args: dict[str, Any]
    #: 坏调用对应的错误（构造 ApiError 用）
    error_code: str
    http_status: int
    error_message: str
    #: 坏调用重复次数（1 表示失败后一次就纠正；>1 表示持续失败直到熔断）
    fail_times: int
    #: 纠正后的「好调用」（``fail_times==1`` 时使用；为 None 表示不纠正，靠降级收尾）
    good_tool: str | None = None
    good_args: dict[str, Any] | None = None
    good_result: dict[str, Any] | None = None
    #: 触发几轮反思后模型给出的最终文本
    final_text: str = ""
    #: 演练用的用户消息（刻意不含写意图与状态提问特征，避免触发其它纠偏分支）
    message: str = "读取计划信息"


_DRILLS: dict[str, Drill] = {
    DRILL_NOT_FOUND: Drill(
        name=DRILL_NOT_FOUND,
        bad_tool="get_plan",
        bad_args={"planId": "guess-plan"},
        error_code="NOT_FOUND",
        http_status=404,
        error_message="计划不存在",
        fail_times=1,
        good_tool="get_plan",
        good_args={"planId": "plan-real"},
        good_result={"planId": "plan-real", "taskList": [{"taskId": "t1", "taskName": "数据库设计"}]},
        final_text="已取到计划 plan-real 的真实任务清单（1 项），后续基于该数据作答。",
    ),
    DRILL_ENGINE_UNAVAILABLE: Drill(
        name=DRILL_ENGINE_UNAVAILABLE,
        bad_tool="risk_scan",
        bad_args={"planId": "plan-real"},
        error_code="ENGINE_UNAVAILABLE",
        http_status=503,
        error_message="算法引擎暂不可用",
        fail_times=3,
        final_text="内部算法引擎暂时不可用，本次未取得风险计算结果；请稍后重试，我不会给出未经计算的结论。",
    ),
}


def drill_by_name(name: str) -> Drill | None:
    return _DRILLS.get(name)


class DrillDispatcher:
    """按剧本响应（或报错）的桩调度器。

    ``fail_times`` 次调用返回错误，之后若剧本给了 ``good_*`` 则返回成功结果；
    没有 ``good_*`` 的剧本（熔断剧本）始终报错 —— 这正是要验证「熔断后不再空转」的场景。
    """

    def __init__(self, drill: Drill) -> None:
        self._drill = drill
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def dispatch(self, name: str, args: dict[str, Any]) -> Any:
        self.calls.append((name, dict(args)))
        if name == self._drill.bad_tool and len([c for c in self.calls if c[0] == self._drill.bad_tool]) <= self._drill.fail_times:
            raise ApiError(self._drill.http_status, self._drill.error_code, self._drill.error_message)
        if self._drill.good_tool and name == self._drill.good_tool:
            return dict(self._drill.good_result or {})
        raise not_found(f"演练剧本未定义工具 {name} 的响应")

    async def fetch_tasks(self, plan_id: str) -> list[dict[str, Any]]:
        return []


class ScriptedLlm:
    """按剧本顺序返回 assistant 消息的确定性模型（仅用于演练）。"""

    name = "drill-scripted"

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []

    async def chat(self, messages, *, tools=None, temperature=0.2) -> dict[str, Any]:
        self.calls.append(list(messages))
        if not self._responses:
            # 剧本用尽后返回一句收尾文本，而不是抛错：演练要验证的是编排链路，
            # 「脚本写少了」属于用例自身缺陷，不应表现为 Agent 崩溃
            return {"role": "assistant", "content": "演练剧本已结束。"}
        return self._responses.pop(0)


def _tool_call(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def build_script(drill: Drill) -> list[dict[str, Any]]:
    """把剧本翻译为脚本化模型的响应序列。"""
    script: list[dict[str, Any]] = []
    for index in range(drill.fail_times):
        script.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [_tool_call(drill.bad_tool, drill.bad_args, f"call_bad_{index}")],
            }
        )
    if drill.good_tool and drill.good_args is not None:
        script.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [_tool_call(drill.good_tool, drill.good_args, "call_good")],
            }
        )
    script.append({"role": "assistant", "content": drill.final_text})
    return script


async def run_drill(drill: Drill, *, max_reflect_retries: int = 2) -> dict[str, Any]:
    """执行一次演练，返回终态结果（``AgentRunResult`` 的字段）与桩调度器调用轨迹。"""
    dispatcher = DrillDispatcher(drill)
    llm = ScriptedLlm(build_script(drill))
    runner = AgentRunner(
        llm,
        dispatcher,  # type: ignore[arg-type] - 桩调度器只需实现 dispatch 契约
        store=InMemorySessionStore(),
        reflect_enabled=True,
        reflect_max_retries=max_reflect_retries,
    )
    result = await runner.run_chat(f"drill-{drill.name}", drill.message, defer_writes=False)
    return {"result": result, "dispatcher": dispatcher}


__all__ = [
    "Drill",
    "DrillDispatcher",
    "ScriptedLlm",
    "DRILL_NAMES",
    "DRILL_NOT_FOUND",
    "DRILL_ENGINE_UNAVAILABLE",
    "build_script",
    "drill_by_name",
    "run_drill",
]

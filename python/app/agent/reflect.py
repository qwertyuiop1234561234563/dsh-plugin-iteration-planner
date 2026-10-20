"""自反思自愈执行循环（结构化归因 + 有界重试 + 熔断降级）。

传统 function calling 循环只会把工具错误**原样回填**给模型，是否纠偏、如何纠偏全凭
模型临场发挥：同一处参数错误可以来回重试 8 步把预算烧光，也可以直接吞掉错误编一个
「已完成」的答复。本模块给循环加上一个**确定性的反思阶段**：

1. **归因（classify）**：把「失败 / 空结果」映射为固定枚举
   （:data:`app.constants.REFLECT_CATEGORIES`）与恢复策略（``REFLECT_STRATEGIES``）。
   归因由服务端规则产出，模型无权自定义——否则「自反思」就退化成又一段模型自述，
   既不可复算也不可断言。
2. **有界重试（bounded）**：按「工具 + 归因类别」签名计数，超过上限即**熔断**该签名，
   改为下发确定性降级指令（不再无脑重试），避免故障放大与步数空转。
3. **可观测（trace）**：全过程产出 :class:`ReflectionEvent` 轨迹（SSE ``reflect`` 事件
   + 终态结果携带），前端据此渲染「自我诊断 → 恢复动作 → 是否恢复」时间线。

设计对齐 OpenAI Agents SDK 的 guardrail 重试与 Reflexion / CRITIC 的「带外部反馈再试」
思路，但**不使用模型自评**：反思结论必须可由工具结果与错误码直接推出。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..constants import (
    REFLECT_CATEGORY_CYCLE,
    REFLECT_CATEGORY_EMPTY,
    REFLECT_CATEGORY_ENGINE,
    REFLECT_CATEGORY_FORMAT,
    REFLECT_CATEGORY_INVALID_ARGS,
    REFLECT_CATEGORY_NOT_FOUND,
    REFLECT_CATEGORY_UNKNOWN,
    REFLECT_STRATEGY_ASK,
    REFLECT_STRATEGY_DEGRADE,
    REFLECT_STRATEGY_REROUTE,
    REFLECT_STRATEGY_RETRY,
)
from ..metrics import METRICS
from ..models import ReflectionEvent, ToolCallRecord

#: 稳定错误码 → 归因类别（错误码是服务端的稳定契约，比异常文案可靠：
#: 文案会被改写、会被本地化，错误码不会）
CATEGORY_BY_CODE: dict[str, str] = {
    "VALIDATION_ERROR": REFLECT_CATEGORY_INVALID_ARGS,
    "NOT_FOUND": REFLECT_CATEGORY_NOT_FOUND,
    "CYCLE_DEPENDENCY": REFLECT_CATEGORY_CYCLE,
    "ENGINE_UNAVAILABLE": REFLECT_CATEGORY_ENGINE,
    "ENGINE_TIMEOUT": REFLECT_CATEGORY_ENGINE,
    "ENGINE_CIRCUIT_OPEN": REFLECT_CATEGORY_ENGINE,
    "LLM_UNAVAILABLE": REFLECT_CATEGORY_ENGINE,
    "LLM_GATEWAY_ERROR": REFLECT_CATEGORY_FORMAT,
    "LLM_FORMAT_ERROR": REFLECT_CATEGORY_FORMAT,
    "TOOL_EXECUTION_ERROR": REFLECT_CATEGORY_UNKNOWN,
    "SESSION_BUSY": REFLECT_CATEGORY_ENGINE,
    "STORAGE_UNAVAILABLE": REFLECT_CATEGORY_ENGINE,
    "SESSION_STORE_UNAVAILABLE": REFLECT_CATEGORY_ENGINE,
    "EMBEDDING_UNAVAILABLE": REFLECT_CATEGORY_ENGINE,
    "VECTOR_STORE_UNAVAILABLE": REFLECT_CATEGORY_ENGINE,
}

#: 归因类别 → 首选恢复策略（确定性映射，不交给模型选择）
STRATEGY_BY_CATEGORY: dict[str, str] = {
    REFLECT_CATEGORY_INVALID_ARGS: REFLECT_STRATEGY_RETRY,
    REFLECT_CATEGORY_NOT_FOUND: REFLECT_STRATEGY_REROUTE,
    REFLECT_CATEGORY_CYCLE: REFLECT_STRATEGY_RETRY,
    REFLECT_CATEGORY_FORMAT: REFLECT_STRATEGY_RETRY,
    REFLECT_CATEGORY_EMPTY: REFLECT_STRATEGY_REROUTE,
    REFLECT_CATEGORY_ENGINE: REFLECT_STRATEGY_RETRY,
    REFLECT_CATEGORY_UNKNOWN: REFLECT_STRATEGY_ASK,
}

#: 归因类别 → 中文标题（前端时间线展示）
CATEGORY_LABELS: dict[str, str] = {
    REFLECT_CATEGORY_INVALID_ARGS: "入参不合法",
    REFLECT_CATEGORY_NOT_FOUND: "目标不存在",
    REFLECT_CATEGORY_CYCLE: "依赖成环",
    REFLECT_CATEGORY_ENGINE: "依赖不可达",
    REFLECT_CATEGORY_FORMAT: "输出格式异常",
    REFLECT_CATEGORY_EMPTY: "检索结果为空",
    REFLECT_CATEGORY_UNKNOWN: "未归因失败",
}

#: 归因类别 → 回填给模型的纠偏指令（**具体、可执行**，而非「请重试」这类空话）
_HINTS: dict[str, str] = {
    REFLECT_CATEGORY_INVALID_ARGS: (
        "自我诊断：上一轮工具入参不合法（{detail}）。请补齐缺失参数并修正类型/取值后重试同一工具；"
        "若你需要先了解可用范围，可先调用 list_plans / get_plan 取到真实值。"
    ),
    REFLECT_CATEGORY_NOT_FOUND: (
        "自我诊断：你引用的目标不存在（{detail}）。**不要凭位置或名称猜测 ID**——"
        "先调用 get_plan 取得真实 taskId，或调用 list_plans 确认正确的 planId，然后再重试。"
    ),
    REFLECT_CATEGORY_CYCLE: (
        "自我诊断：依赖结构成环（{detail}）。请调整 predecessorTaskIds，去掉造成环的那条依赖，"
        "再重试；不要把同一条依赖原样再提交一次。"
    ),
    REFLECT_CATEGORY_FORMAT: (
        "自我诊断：依赖返回格式/协议异常（{detail}）。请严格按工具 schema 输出结构化 tool_calls，"
        "不要用自然语言或 JSON 代码块描述调用。"
    ),
    REFLECT_CATEGORY_EMPTY: (
        "自我诊断：工具调用成功但**没有取到任何数据**（{detail}）。改用另一种方式获取："
        "换更宽泛的检索词重新 kb_search，或改用 list_plans / get_plan 直接读取事实；"
        "确实不存在时再如实告知用户，不要编造内容。"
    ),
    REFLECT_CATEGORY_ENGINE: (
        "自我诊断：内部算法引擎暂时不可达（{detail}）。可原样重试一次；"
        "若仍失败，请如实说明「引擎暂不可用，本次未取得计算结果」，**不要编造排期数字**。"
    ),
    REFLECT_CATEGORY_UNKNOWN: (
        "自我诊断：工具执行出现未预期异常（{detail}）。请先确认入参是否指向真实存在的计划 / 任务；"
        "若仍失败，请向用户说明受阻原因并请求澄清，不要重复提交同一调用。"
    ),
}

#: 熔断后的降级指令（不再重试，要求模型给出确定性说明）
_DEGRADE_HINT = (
    "自我诊断：同一处失败（{category}）已连续出现 {attempt} 次，**已熔断，不再重试**。"
    "请立刻改用确定性说法收尾：说明你尝试了什么、在哪里受阻（{detail}），"
    "并明确告知用户「本次未能完成，需要您补充或确认什么信息」；严禁编造结果或声称已完成。"
)

#: 仅这些工具「结果为空」才构成需要自愈的信号：它们是**取数**类工具，空结果说明取错了对象
#: 或检索词不匹配。反例：``risk_scan`` 返回空是「没有风险」这一**好结果**，不得触发重试；
#: 把它当作失败会诱导模型反复重扫，纯属浪费步数。
EMPTY_SENSITIVE_TOOLS: frozenset[str] = frozenset(
    {"kb_search", "kb_list_documents", "memory_search", "list_plans", "get_plan"}
)


def classify_error(code: str | None, message: str | None = None) -> str:
    """按稳定错误码归因（未知码回落到 :data:`REFLECT_CATEGORY_UNKNOWN`）。"""
    if code and code in CATEGORY_BY_CODE:
        return CATEGORY_BY_CODE[code]
    text = (message or "").lower()
    if "timed out" in text or "timeout" in text:
        return REFLECT_CATEGORY_ENGINE
    if "不存在" in (message or "") or "not found" in text:
        return REFLECT_CATEGORY_NOT_FOUND
    return REFLECT_CATEGORY_UNKNOWN


def is_empty_result(name: str, result: object) -> bool:
    """判断「工具成功但没取到数据」——仅对取数类工具生效（见 ``EMPTY_SENSITIVE_TOOLS``）。"""
    if name not in EMPTY_SENSITIVE_TOOLS:
        return False
    if isinstance(result, dict):
        if "count" in result and isinstance(result["count"], int):
            return result["count"] == 0 and not _has_payload(result)
        if name == "get_plan":
            tasks = result.get("taskList")
            return isinstance(tasks, list) and not tasks
        return all(_is_blank(value) for value in result.values())
    if isinstance(result, list):
        return not result
    return result in (None, "", 0)


def _has_payload(result: dict) -> bool:
    """除 ``count`` 外是否还有真实载荷（有载荷说明不是空结果）。"""
    for key, value in result.items():
        if key == "count":
            continue
        if value:
            return True
    return False


def _is_blank(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, (str, list, dict, tuple, set)):
        return not value
    if isinstance(value, (int, float)):
        return value == 0
    return False


def _signature(tool: str, category: str) -> str:
    return f"{tool}|{category}"


@dataclass
class ReflectionTracker:
    """有界反思记录器：一次编排内共享（跨步累计签名计数与轨迹）。

    ``max_retries`` 是**同一签名**的重试上限：超过即熔断该签名，改为降级指令。
    不同签名各自计数，互不牵连（「A 工具参数错」不应导致「B 工具引擎超时」也被熔断）。
    """

    max_retries: int = 2
    enabled: bool = True
    _counts: dict[str, int] = field(default_factory=dict)
    _events: list[ReflectionEvent] = field(default_factory=list)
    #: 尚未观测到「恢复」的事件下标（签名 → 事件下标），供后续步骤回填 ``recovered``
    _awaiting: dict[str, int] = field(default_factory=dict)

    @property
    def events(self) -> list[ReflectionEvent]:
        return list(self._events)

    def observe_step(
        self, step: int, records: list[ToolCallRecord]
    ) -> tuple[list[str], list[ReflectionEvent]]:
        """对**一步**的工具结果做归因，返回 ``(回填提示, 新增反思事件)``。

        同一步内多个失败会被合并为一次回填（每个签名一条提示），避免一步刷出多条
        高度重复的纠偏消息、把上下文预算吃光。
        """
        if not self.enabled or not records:
            return [], []
        fresh: list[ReflectionEvent] = []
        hints: list[str] = []

        # 先结算「恢复」：本步成功的工具，可把同工具此前未恢复的反思标记为已恢复
        for record in records:
            if record.ok and not is_empty_result(record.name, record.result):
                index = self._awaiting.pop(record.name, None)
                if index is not None:
                    self._events[index].recovered = True
                    METRICS.inc_counter("agent_reflect_total", {"category": self._events[index].category, "outcome": "recovered"})

        for record in records:
            category = None
            detail = ""
            if not record.ok:
                category = classify_error(record.code, record.error)
                detail = record.error or "工具返回失败"
            elif is_empty_result(record.name, record.result):
                category = REFLECT_CATEGORY_EMPTY
                detail = "工具返回空数据集"

            if category is None:
                continue

            signature = _signature(record.name, category)
            self._counts[signature] = self._counts.get(signature, 0) + 1
            attempt = self._counts[signature]
            tripped = attempt > self.max_retries
            strategy = REFLECT_STRATEGY_DEGRADE if tripped else STRATEGY_BY_CATEGORY[category]

            event = ReflectionEvent(
                index=len(self._events) + 1,
                step=step,
                tool=record.name,
                category=category,
                strategy=strategy,
                attempt=attempt,
                signature=signature,
                title=f"{CATEGORY_LABELS.get(category, category)}"
                + (f"（第 {attempt} 次）" if attempt > 1 else ""),
                detail=detail[:300],
                at=time.time(),
            )
            self._events.append(event)
            fresh.append(event)
            METRICS.inc_counter(
                "agent_reflect_total",
                {"category": category, "outcome": strategy},
                help="自反思自愈次数（按归因类别与恢复策略分标签）",
            )

            if tripped:
                METRICS.inc_counter("agent_reflect_tripped_total", {"signature": record.name})
                hints.append(_DEGRADE_HINT.format(category=CATEGORY_LABELS.get(category, category), attempt=attempt, detail=event.detail))
            else:
                # 记录「待恢复」：同工具下一次成功即视为自愈成功
                self._awaiting.setdefault(record.name, len(self._events) - 1)
                hints.append(_HINTS[category].format(detail=event.detail))
        return hints, fresh


__all__ = [
    "ReflectionTracker",
    "CATEGORY_BY_CODE",
    "STRATEGY_BY_CATEGORY",
    "CATEGORY_LABELS",
    "EMPTY_SENSITIVE_TOOLS",
    "classify_error",
    "is_empty_result",
]

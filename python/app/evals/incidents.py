"""由失败轨迹**确定性推导**回归用例（事故 → 用例 → 门禁 闭环的中间一环）。

一次事故的价值在于「它不能再发生第二次」。但把事故转成回归用例通常靠人手工写断言——
这条链路最容易断在「事后没人有动力补用例」上。

本模块去掉这个人工环节：直接读一份 :class:`app.models.AgentTrace`，按**确定性规则**推导
断言。规则只依据轨迹里的客观事实，不依赖任何模型判断：

============================  ==========================================================
轨迹中的事实                推导出的断言（回归要求）
============================  ==========================================================
存在失败的工具调用           ``noFailedTools: True``（同一需求下不得再出现工具失败）
触发过一致性护栏             ``maxGuards: 0``（谎报完成 / 无依据作答 / 写指令空转必须绝迹）
某工具失败且已归因           ``reflectionCategories: [该类]`` + ``reflectionRecovered: True``
产出过待确认动作             ``requiresPending: True``（修复不得绕过两阶段确认直接落库）
产出过计划                   ``hasPlan: True`` + ``minTasks: 1``
（恒定）                     ``noFalseCompletion: True``（结论不得谎报已完成）
============================  ==========================================================

**为什么断言要「宁松勿误」**：这是一条会被 CI 门禁消费的用例。若断言写死「必须调用
``schedule_calc``」这类过细要求，一次无关的实现调整就会让门禁变红，团队很快学会忽略它。
因此这里只推导**事故当时确实被违反**的那几条不变量，其余交给黄金集。

用例 ID 用 ``inc-<轨迹前 8 位>``：同一条轨迹重复沉淀即覆盖同一条用例（幂等），
不会在集合里堆出重复项。
"""

from __future__ import annotations

import logging
from typing import Any

from ..agent.reflect import classify_error
from ..constants import (
    CUSTOM_CASE_ORIGIN_INCIDENT,
    INCIDENT_CASE_ID_PREFIX,
    TRACE_KIND_REFLECT,
    TRACE_KIND_TOOL,
)
from ..errors import bad_request
from ..models import AgentTrace, CustomEvalCase, EvalCaseSpec

logger = logging.getLogger("iteration_planner.evals.incidents")

#: 断言键 → 人类可读理由模板
_RATIONALE = {
    "noFailedTools": "本次事故中存在失败的工具调用（{tools}）：同一需求重跑时不得再出现工具失败。",
    "maxGuards": "本次事故触发了 {count} 次一致性护栏（谎报完成 / 无依据作答 / 写指令空转）：修复后必须为 0。",
    "noFalseCompletion": "结论不得在没有任何真实写入的情况下宣称已完成（Agent 最危险的失败模式）。",
    "requiresPending": "本次事故中写操作走了两阶段确认：修复不得绕过确认直接落库。",
    "hasPlan": "本次运行产出了计划：重跑应同样产出计划（否则说明修复把主链路打断了）。",
    "minTasks": "计划至少要拆出 1 个任务，否则该计划对用户没有价值。",
}


def derive_case(
    trace: AgentTrace,
    *,
    title: str | None = None,
    note: str = "",
    cleanup: bool = True,
) -> CustomEvalCase:
    """由轨迹推导回归用例（无 LLM、无引擎调用，纯确定性）。"""
    if trace.request is None or not trace.request.message:
        raise bad_request("该轨迹没有保存原始请求，无法沉淀为可重跑的回归用例")
    kind = trace.request.kind
    if kind not in ("plan", "chat"):
        raise bad_request(f"轨迹类型 {kind} 暂不支持沉淀为回归用例")

    expect: dict[str, Any] = {"noFalseCompletion": True}
    rationale: list[str] = [_RATIONALE["noFalseCompletion"]]

    if trace.failedTools:
        expect["noFailedTools"] = True
        rationale.append(_RATIONALE["noFailedTools"].format(tools="、".join(sorted(set(trace.failedTools)))))
    if trace.guardCount > 0:
        expect["maxGuards"] = 0
        rationale.append(_RATIONALE["maxGuards"].format(count=trace.guardCount))

    categories = sorted(
        {
            classify_error(span.code, span.detail)
            for span in trace.spans
            if span.kind == TRACE_KIND_TOOL and span.status == "error"
        }
    )
    if categories:
        expect["reflectionCategories"] = categories
        recovered = any(
            span.kind == TRACE_KIND_REFLECT and span.output.get("recovered") for span in trace.spans
        )
        if recovered:
            expect["reflectionRecovered"] = True
            rationale.append(f"本次事故经自反思恢复了 {('、'.join(categories))} 类失败：该自愈能力不得回退。")
        else:
            rationale.append(f"本次事故的归因类别为 {('、'.join(categories))}：重跑时应能被正确归因。")

    if trace.pendingCount > 0:
        expect["requiresPending"] = True
        rationale.append(_RATIONALE["requiresPending"])
    if trace.planId and kind == "plan":
        expect["hasPlan"] = True
        expect["minTasks"] = 1
        rationale.extend([_RATIONALE["hasPlan"], _RATIONALE["minTasks"]])

    case = EvalCaseSpec(
        id=f"{INCIDENT_CASE_ID_PREFIX}{trace.traceId[:8]}",
        title=title or f"回归：{trace.request.message[:60]}",
        kind=kind,
        input=_build_input(trace),
        expect=expect,
        rubric="",
        cleanup=cleanup if kind == "plan" else False,
    )
    logger.info("由轨迹 %s 推导出回归用例 %s（断言 %d 条）", trace.traceId, case.id, len(expect))
    return CustomEvalCase(
        case=case,
        origin=CUSTOM_CASE_ORIGIN_INCIDENT,
        originTraceId=trace.traceId,
        note=note,
        owner=trace.owner,
        rationale=rationale,
    )


def _build_input(trace: AgentTrace) -> dict[str, Any]:
    """把原始请求还原成用例输入（chat 类用例不复用原会话：会话是新起的，避免串上下文）。"""
    request = trace.request
    assert request is not None  # derive_case 已校验
    if request.kind == "plan":
        payload: dict[str, Any] = {"requirement": request.message}
        if request.iterationDeadline:
            payload["iterationDeadline"] = request.iterationDeadline
        if request.iterationStartDate:
            payload["iterationStartDate"] = request.iterationStartDate
        if request.devMemberCount:
            payload["devMemberCount"] = request.devMemberCount
        return payload
    payload = {"message": request.message}
    if request.planId:
        payload["planId"] = request.planId
    return payload


def replay_payload(trace: AgentTrace) -> dict[str, Any]:
    """回放用的请求参数（前端「按原请求重跑」与调试器跳转共用）。"""
    return {
        "kind": trace.request.kind if trace.request else trace.kind,
        "input": _build_input(trace) if trace.request else {},
        "traceId": trace.traceId,
        "status": trace.status,
        "error": trace.error,
    }


__all__ = ["derive_case", "replay_payload"]

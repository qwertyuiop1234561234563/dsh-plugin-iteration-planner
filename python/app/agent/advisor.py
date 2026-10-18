"""风险缓解建议生成（引擎检出风险 → LLM / 规则派生缓解动作）。

职责边界（与项目硬约束一致）：
- **风险识别**完全由 TypeScript 引擎（``risk_scan`` 规则）负责，本模块只读取
  ``plan.riskList``，不新增、不删除、不改写任何风险条目及其等级/关联任务；
- 本模块只产出「缓解动作」这一**建议内容**：LLM 可用时由模型生成，不可用或输出
  不可解析时降级为关键词规则派生，并把降级原因写入 ``degradedReason``。
"""

from __future__ import annotations

import logging
from typing import Any

from ..engine_client import EngineClient
from ..errors import ApiError
from ..metrics import METRICS
from ..models import AdviceItem, AdviceResponse
from .json_utils import extract_json_object
from .prompts import RISK_ADVISOR_SYSTEM_PROMPT, build_advice_instruction
from .runner import AgentRunner

logger = logging.getLogger("iteration_planner.advisor")

#: 风险描述关键词 → 规则派生动作（仅组织建议内容，不做风险判定）
_RULE_ACTIONS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("循环依赖", "循环", "cycle"),
        (
            "定位依赖环（强连通分量）并打断反向边，明确唯一前置",
            "复核任务拆分是否存在互为前置的过度耦合，必要时拆为子任务",
        ),
    ),
    (
        ("单日工时超限", "过载", "overload"),
        (
            "对该成员做任务削峰：把非关键任务改派给负载较低的成员",
            "拆分粒度过大的任务，或把非关键工作顺延到峰值日期之后",
        ),
    ),
    (
        ("关键路径", "critical"),
        (
            "为关键路径任务追加资源或缩短工时，优先保障按期交付",
            "复核关键任务的前置依赖，识别可并行化的解耦点",
        ),
    ),
    (
        ("孤立", "isolated"),
        (
            "确认该任务是否应挂接前置/后置依赖，补齐 DAG 连接",
            "若为冗余任务，从计划中移除或合并到相邻任务",
        ),
    ),
    (
        ("语义", "semantic", "模糊", "外部依赖", "未就绪", "不确定性"),
        (
            "复核需求描述与任务内容的一致性，补充可验收的完成标准",
            "与需求方确认歧义/外部依赖点后更新任务描述或补充 Mock 降级方案，降低返工概率",
        ),
    ),
)


def _rule_actions(risk: dict[str, Any]) -> list[str]:
    """按风险描述关键词派生缓解动作（规则降级路径）。"""
    text = f"{risk.get('description') or ''} {risk.get('suggest') or ''}".lower()
    actions: list[str] = []
    for keywords, mapped in _RULE_ACTIONS:
        if any(keyword.lower() in text for keyword in keywords):
            actions.extend(mapped)
    task_ids = [str(task) for task in (risk.get("taskIds") or [])]
    if task_ids:
        actions.append("关联任务：" + "、".join(task_ids) + "，安排专人跟进并同步进展")
    if str(risk.get("level") or "").upper() == "HIGH":
        actions.insert(0, "提级为迭代阻塞项，纳入每日站会跟踪直至关闭")
    if not actions:
        suggestion = str(risk.get("suggest") or "").strip()
        actions.append(suggestion or "复核该风险的具体影响范围并制定缓解计划")
    return actions


def _build_items(risks: list[dict[str, Any]], actions_by_risk: dict[str, list[str]]) -> list[AdviceItem]:
    """把引擎风险条目与（LLM 或规则）动作合并为响应条目。"""
    items: list[AdviceItem] = []
    for risk in risks:
        risk_id = str(risk.get("riskId") or "")
        items.append(
            AdviceItem(
                riskId=risk_id or None,
                level=str(risk.get("level") or "LOW"),
                taskIds=[str(task) for task in (risk.get("taskIds") or [])],
                description=str(risk.get("description") or ""),
                suggestion=str(risk.get("suggest") or ""),
                actions=actions_by_risk.get(risk_id) or _rule_actions(risk),
            )
        )
    return items


async def generate_advice(
    engine: EngineClient,
    runner: AgentRunner,
    *,
    model: str | None,
    plan_id: str,
) -> AdviceResponse:
    """读取引擎风险清单并生成缓解建议（LLM 优先，失败降级规则模式）。"""
    plan = await engine.request("GET", f"/plans/{plan_id}")
    risks: list[dict[str, Any]] = [r for r in (plan.get("riskList") or []) if isinstance(r, dict)]
    if not risks:
        return AdviceResponse(
            mode="rule",
            summary="当前计划未检出风险，无需缓解建议。",
            items=[],
            model=None,
        )

    actions_by_risk: dict[str, list[str]] = {}
    summary: str | None = None
    degraded: str | None = None
    mode = "rule"

    if runner.available:
        try:
            text = await runner.complete(
                [
                    {"role": "system", "content": RISK_ADVISOR_SYSTEM_PROMPT},
                    {"role": "user", "content": build_advice_instruction(plan)},
                ]
            )
            parsed = extract_json_object(text)
            if parsed is None or not isinstance(parsed.get("items"), list):
                raise ApiError(422, "LLM_FORMAT_ERROR", "建议输出不是合法的 JSON 结构")
            for entry in parsed["items"]:
                if not isinstance(entry, dict):
                    continue
                risk_id = str(entry.get("riskId") or "")
                actions = [str(action).strip() for action in (entry.get("actions") or []) if str(action).strip()]
                if risk_id and actions:
                    actions_by_risk[risk_id] = actions
            if not actions_by_risk:
                raise ApiError(422, "LLM_FORMAT_ERROR", "建议输出未包含任何可用缓解动作")
            summary = str(parsed.get("summary") or "").strip() or None
            mode = "llm"
        except ApiError as exc:
            degraded = f"LLM 建议生成失败（{exc.code}），已回退规则模式"
            logger.warning("风险建议回退规则模式：%s", exc.message)
        except Exception as exc:  # noqa: BLE001 - 建议生成失败不应影响端点可用性
            degraded = f"LLM 建议生成异常，已回退规则模式：{exc}"
            logger.exception("风险建议生成出现未预期异常")

    items = _build_items(risks, actions_by_risk)
    if summary is None:
        high = sum(1 for risk in risks if str(risk.get("level")).upper() == "HIGH")
        summary = f"共检出 {len(risks)} 条风险（高危 {high} 条），已按等级与影响范围给出缓解动作。"

    METRICS.inc_counter("advisor_runs_total", {"mode": mode})
    return AdviceResponse(mode=mode, summary=summary, items=items, model=model, degradedReason=degraded)


__all__ = ["generate_advice"]
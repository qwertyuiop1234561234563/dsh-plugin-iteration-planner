"""迭代复盘 + 学习（复盘结论写回长期记忆，反哺后续规划）。

参考 LangGraph 的 reflection 模式与同类企业项目的 retro 实践：一次迭代结束后，把
「实际 vs 预估」的偏差沉淀为**可复用的校准口径**，下一轮规划时由长期记忆自动带出。

数据来源边界（重要）：

- **交付偏移 / 进度 / 范围变更**：完全由引擎事实与配置统计得出（确定性、可复算）；
- **工时偏差与估算校准**：需要**实际工时**，而引擎计划模型中并没有该字段（不得凭空推断），
  因此只能由调用方提供；未提供时明确标注 ``effort.available=false`` 并说明原因，绝不编造；
- **风险兑现率**：需要人工判定「确实发生了」的风险 ID，同时给出**可推断候选**（关联任务全部
  处于 DELAYED / BLOCKED 的高风险项）作为提示，仅作参考，不冒充兑现率；
- **结论与校准口径**：LLM 优先，未配置 / 解析失败时降级为确定性规则结论（明确标注 ``mode=rule``）。
"""

from __future__ import annotations

import logging
from typing import Any

from ..constants import RETRO_MEMORY_SOURCE
from ..engine_client import EngineClient
from ..errors import ApiError
from ..metrics import METRICS
from ..models import RetroResponse
from .analytics import retro_metrics
from .json_utils import extract_json_object
from .memory import MemoryService
from .prompts import RETRO_ANALYST_SYSTEM_PROMPT, build_retro_instruction
from .runner import AgentRunner

logger = logging.getLogger("iteration_planner.retro")

#: 平均工时偏差超过该比例（实际/预估）才提示「系统性超支」
OVERRUN_WARN_RATIO = 1.15
#: 平均工时偏差低于该比例才提示「系统性低估」
UNDERRUN_WARN_RATIO = 0.85


def _rule_conclusions(metrics: dict[str, Any]) -> dict[str, Any]:
    """确定性规则结论（模型不可用时的降级路径，全部基于统计事实）。"""
    highlights: list[str] = []
    problems: list[str] = []
    actions: list[str] = []

    progress = metrics.get("progress") or {}
    delivery = metrics.get("delivery") or {}
    effort = metrics.get("effort") or {}
    risks = metrics.get("risks") or {}

    done_ratio = progress.get("doneRatio")
    if isinstance(done_ratio, (int, float)):
        text = f"任务完成度（按工时）{round(float(done_ratio) * 100, 1)}%"
        if float(done_ratio) >= 1:
            highlights.append(f"{text}，迭代范围全部交付")
        else:
            problems.append(f"{text}，仍有未完成任务")

    offset = delivery.get("offsetDays")
    if isinstance(offset, int):
        if offset > 0:
            problems.append(f"引擎推算完成日期比截止日期晚 {offset} 天，交付存在延期")
            actions.append("对关键路径任务做资源倾斜或拆分，必要时与需求方重谈范围")
        elif offset < 0:
            highlights.append(f"提前 {abs(offset)} 天完成排期，可考虑提前拉入下轮需求")

    average_ratio = effort.get("averageRatio")
    if isinstance(average_ratio, (int, float)):
        if float(average_ratio) > OVERRUN_WARN_RATIO:
            ratio_text = f"{round(float(average_ratio), 2)} 倍"
            problems.append(f"实际工时平均为预估的 {ratio_text}，估算偏低")
            actions.append(f"下一轮同类任务按 {ratio_text} 系数上调预估工时，并在计划中预留缓冲")
        elif float(average_ratio) < UNDERRUN_WARN_RATIO:
            problems.append("实际工时明显低于预估，可能存在过度预留")

    rate = risks.get("materializedRate")
    if isinstance(rate, (int, float)):
        if float(rate) >= 0.5:
            problems.append(f"风险兑现率 {round(float(rate) * 100, 1)}%，风险识别偏保守")
            actions.append("复盘已兑现风险的前置信号，前移到更早的检查点捕获")
        else:
            highlights.append(f"风险兑现率 {round(float(rate) * 100, 1)}%，识别精度较好")

    change_count = (metrics.get("scope") or {}).get("changeCount") or 0
    if change_count:
        problems.append(f"迭代内发生 {change_count} 处范围变更")

    if not actions:
        actions.append("保持当前估算与排期口径，继续按本轮节奏推进下一迭代")

    return {
        "summary": "、".join(highlights[:2] + problems[:2]) or "本轮无可用的量化结论（缺少实际工时等外部事实）",
        "highlights": highlights,
        "problems": problems,
        "actions": actions,
        "memories": _rule_memories(metrics),
    }


def _rule_memories(metrics: dict[str, Any]) -> list[str]:
    """从统计事实派生**可长期复用**的校准口径（不含一次性数字）。"""
    out: list[str] = []
    average_ratio = (metrics.get("effort") or {}).get("averageRatio")
    if isinstance(average_ratio, (int, float)):
        rounded = round(float(average_ratio), 2)
        out.append(f"本团队本轮实际工时平均为预估的 {rounded} 倍，下一轮同类任务按该系数校准预估")
    offset = (metrics.get("delivery") or {}).get("offsetDays")
    if isinstance(offset, int) and offset > 0:
        out.append(f"本团队排期普遍偏乐观，实际完成日期比引擎推算晚约 {offset} 天，需在截止日期前预留缓冲")
    rate = (metrics.get("risks") or {}).get("materializedRate")
    if isinstance(rate, (int, float)) and float(rate) >= 0.5:
        out.append("本团队已识别风险的兑现比例偏高，规划阶段需对高风险项预留专项时间")
    return out


async def run_retro(
    engine: EngineClient,
    runner: AgentRunner,
    *,
    plan_id: str,
    actual_hours: dict[str, Any] | None = None,
    materialized_risk_ids: list[str] | None = None,
    scope_changes: list[str] | None = None,
    write_memory: bool = True,
    memory: MemoryService | None = None,
    max_memories: int = 4,
    model: str | None = None,
) -> RetroResponse:
    """执行一次迭代复盘（统计 + 结论 + 可选记忆回写）。"""
    plan = await engine.request("GET", f"/plans/{plan_id}")
    metrics = retro_metrics(
        plan,
        actual_hours=actual_hours or {},
        materialized_risk_ids=materialized_risk_ids or [],
        scope_changes=scope_changes or [],
    )

    mode = "rule"
    degraded: str | None = None
    conclusion: dict[str, Any] | None = None
    if runner.available:
        try:
            text = await runner.complete(
                [
                    {"role": "system", "content": RETRO_ANALYST_SYSTEM_PROMPT},
                    {"role": "user", "content": build_retro_instruction(plan, metrics)},
                ],
                temperature=0.2,
            )
            parsed = extract_json_object(text)
            if parsed is None or not isinstance(parsed.get("summary"), str):
                raise ApiError(422, "LLM_FORMAT_ERROR", "复盘输出不是合法 JSON 结构")
            conclusion = {
                "summary": str(parsed["summary"]),
                "highlights": [str(item) for item in (parsed.get("highlights") or []) if str(item).strip()],
                "problems": [str(item) for item in (parsed.get("problems") or []) if str(item).strip()],
                "actions": [str(item) for item in (parsed.get("actions") or []) if str(item).strip()],
                "memories": [str(item) for item in (parsed.get("memories") or []) if str(item).strip()],
            }
            mode = "llm"
        except ApiError as exc:
            degraded = f"LLM 复盘失败（{exc.code}），已回退规则结论"
            logger.warning("复盘回退规则结论：%s", exc.message)
        except Exception:  # noqa: BLE001 - 结论生成失败不应使端点不可用
            degraded = "LLM 复盘异常，已回退规则结论"
            logger.exception("复盘结论生成出现未预期异常")
    else:
        degraded = "未配置 LLM，已使用规则结论"

    if conclusion is None:
        conclusion = _rule_conclusions(metrics)

    written = 0
    memory_enabled = memory is not None
    memories = [item for item in conclusion.get("memories") or [] if item][: max(1, max_memories)]
    if write_memory and memory is not None and memories:
        written = await _write_memories(memory, plan_id, memories)
    elif write_memory and memory is None:
        degraded = f"{degraded or ''}；长期记忆未启用，复盘结论未写回".lstrip("；")

    METRICS.inc_counter("retro_runs_total", {"mode": mode})
    METRICS.inc_counter("retro_memories_total", {"outcome": "written"}, value=float(written))
    return RetroResponse(
        planId=plan_id,
        metrics=metrics,
        mode=mode,
        model=model if mode == "llm" else None,
        degradedReason=degraded,
        summary=str(conclusion.get("summary") or ""),
        highlights=list(conclusion.get("highlights") or []),
        problems=list(conclusion.get("problems") or []),
        actions=list(conclusion.get("actions") or []),
        memories=memories,
        memoryWritten=written,
        memoryEnabled=memory_enabled,
    )


async def _write_memories(memory: MemoryService, plan_id: str, texts: list[str]) -> int:
    """把校准结论写入长期记忆；单条失败只告警（不影响复盘结果返回）。"""
    written = 0
    for text in texts:
        try:
            result = await memory.add_memory(
                text,
                memory_type="fact",
                importance="medium",
                tags=["retro", "calibration"],
                source=f"{RETRO_MEMORY_SOURCE}:{plan_id}"[:128],
            )
            if result.get("created"):
                written += 1
        except ApiError as exc:
            logger.warning("复盘结论写入长期记忆失败：%s", exc.message)
        except Exception:  # noqa: BLE001 - 记忆写入失败不应影响复盘本身
            logger.exception("复盘结论写入长期记忆出现未预期异常")
    return written


__all__ = ["run_retro", "OVERRUN_WARN_RATIO", "UNDERRUN_WARN_RATIO"]

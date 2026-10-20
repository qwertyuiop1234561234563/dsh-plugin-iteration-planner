"""LLM-as-judge 评分轨：只对**自然语言结论质量**打分，不参与事实判定。

分工必须说清楚：**事实由确定性断言判定**（有没有真的调引擎、有没有把写操作登记为待确认、
有没有在无写入的情况下谎报完成），**表达由 judge 打分**（结论是否切题、是否说明了依据与
影响、是否可据以行动）。把事实判断交给模型是危险的——「像模像样的空话」最容易骗过模型评委，
而那恰恰是 Agent 最容易犯的错。

因此本模块的评分只作为**补充信号**（权重 0.4，见 ``EVAL_WEIGHT_JUDGE``）：judge 不可用
（未配置模型 / 调用失败）时整条评分轨直接缺席，综合分退回纯确定性分，评测与门禁照常运行。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..constants import EVAL_JUDGE_LEVELS
from ..errors import ApiError
from ..metrics import METRICS

logger = logging.getLogger("iteration_planner.evals.judge")

#: judge 的产出上限（防止评委自己写一篇长文把评测成本与预算吃光）
_MAX_JUDGE_CHARS = 600

#: 评委系统提示：**要求给分并给理由，且只针对结论质量**。明确禁止它评价工具调用/流程，
#: 那部分已有确定性断言覆盖；否则会出现「流程全对但 judge 给低分」的噪声，误导排查。
_JUDGE_SYSTEM = (
    "你是研发迭代规划的评审专家。请仅针对**助手结论的质量**打分，不要评价工具调用、流程或系统实现。\n"
    "评分维度：是否切题、是否给出了具体可执行的内容、是否说明了依据与影响、是否诚实说明未完成的部分。\n"
    "严格按 JSON 输出：{\"score\": 0~1 的数字, \"rationale\": \"不超过 80 字的理由\"}。不要输出其它内容。"
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def level_for(score: float) -> str:
    """把 0~1 连续分映射为可读等级（阈值集中在 ``app.constants.EVAL_JUDGE_LEVELS``）。"""
    for name, threshold in EVAL_JUDGE_LEVELS:
        if score >= threshold:
            return name
    return EVAL_JUDGE_LEVELS[-1][0]


def _parse(raw: str) -> tuple[float, str] | None:
    """从评委输出里解析 ``{score, rationale}``（允许被 ```json 代码块包裹）。"""
    match = _JSON_RE.search(raw or "")
    if match is None:
        return None
    try:
        payload = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        score = float(payload.get("score"))
    except (TypeError, ValueError):
        return None
    # 把越界分数夹到 [0, 1]：评委偶发给出 0~10 分制，越界值直接进入加权会污染平均分
    score = max(0.0, min(1.0, score))
    return score, str(payload.get("rationale") or "").strip()[:200]


async def judge_case(runner: Any, *, rubric: str, question: str, answer: str) -> tuple[float, str, str] | None:
    """对一条用例的结论打分。

    :return: ``(分数, 等级, 理由)``；评委不可用 / 输出不可解析时返回 ``None``，
        由调用方把 judge 轨标记为缺席（而不是给 0 分——0 分会被误读为「质量极差」）。
    """
    if not rubric.strip():
        return None
    instruction = (
        f"【评分标准】\n{rubric.strip()}\n\n"
        f"【用户请求】\n{question.strip()[:2000]}\n\n"
        f"【助手结论】\n{(answer or '(空)').strip()[:4000]}"
    )
    try:
        text = await runner.complete(
            [{"role": "system", "content": _JUDGE_SYSTEM}, {"role": "user", "content": instruction}],
            temperature=0.0,
        )
    except ApiError as exc:
        logger.info("评测评委不可用（%s），本轮仅使用确定性评分", exc.code)
        METRICS.inc_counter("eval_judge_total", {"outcome": exc.code})
        return None
    except Exception:  # noqa: BLE001 - 评委是可选轨，任何异常都不应让评测整体失败
        logger.exception("评测评委调用异常，已跳过 judge 轨")
        METRICS.inc_counter("eval_judge_total", {"outcome": "error"})
        return None

    parsed = _parse(text[:_MAX_JUDGE_CHARS])
    if parsed is None:
        METRICS.inc_counter("eval_judge_total", {"outcome": "unparsable"})
        logger.warning("评测评委输出无法解析为 JSON：%s", (text or "")[:200])
        return None
    score, rationale = parsed
    METRICS.inc_counter("eval_judge_total", {"outcome": "ok"})
    METRICS.observe("eval_judge_score", score, buckets=(0.25, 0.5, 0.75, 0.9), help="评测评委打分分布")
    return score, level_for(score), rationale


__all__ = ["judge_case", "level_for"]

"""动态上下文预算管理（分区预算 + 旧轮次衰减压缩）。

朴素做法是「超预算就从最旧的用户回合开始删」（见本仓库早期实现的 ``_enforce_budget``）。
它会**先删掉最该保留的东西**：删掉「待确认动作已登记、尚未落库」的回填结果，模型下一轮就
不知道自己刚刚登记过什么；删掉反思纠偏提示，模型立刻退回未纠偏状态、把同一个错误再犯一遍。

本模块把上下文切成**五个分区**，按「先固定占位、再分配弹性区」的顺序分配：

==============  ===========================================================
分区            策略
==============  ===========================================================
``system``      系统提示词：固定，永不裁剪（它是行为契约）
``keep``        必保区：纠偏提示 / 反思回填 / 工具回填（含待确认与失败）——本轮的
                「工作区」；删它等于让模型丢掉「本轮为什么重试」，因此永不裁剪
``current``     本轮用户输入：固定，永不裁剪
``recall``      检索增强区块（记忆 / 知识库）：按剩余额度截断（先丢靠后的区块）
``history``     历史会话回合：按衰减压缩（旧轮次折叠为一行摘要，仍超才逐轮丢弃）
==============  ===========================================================

分区边界由**确定性规则**划定（见 :func:`classify_zones`），并逐分区回传用量明细
（:class:`app.models.BudgetReport`），信封与 SSE 一并下发，前端据此渲染实时预算条——
「上下文为什么被裁掉」必须是可解释、可观测的，否则调大预算只能靠猜。
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any, Iterable

from ..constants import (
    BUDGET_KEEP_RECENT_TURNS,
    BUDGET_SUMMARY_CHARS,
    BUDGET_ZONE_CURRENT,
    BUDGET_ZONE_HISTORY,
    BUDGET_ZONE_KEEP,
    BUDGET_ZONE_LABELS,
    BUDGET_ZONE_RECALL,
    BUDGET_ZONE_SYSTEM,
    BUDGET_ZONES,
)
from ..metrics import METRICS
from ..models import BudgetReport, BudgetZoneUsage

#: 压缩摘要的哨兵前缀：既是**人类可读**标记，也是下一轮分类的依据（见 :func:`classify_zones`）
SUMMARY_PREFIX = "（历史对话摘要，以下轮次已压缩）："


def message_chars(message: dict[str, Any]) -> int:
    """单条消息的字符规模（正文 + ``tool_calls`` 序列化长度）。"""
    total = 0
    content = message.get("content")
    if isinstance(content, str):
        total += len(content)
    tool_calls = message.get("tool_calls")
    if tool_calls:
        total += len(json.dumps(tool_calls, ensure_ascii=False, default=str))
    return total


def estimate_chars(messages: Iterable[dict[str, Any]]) -> int:
    """消息列表的字符规模（约 4 字符 ≈ 1 token）。"""
    return sum(message_chars(message) for message in messages)


def _text_of(message: dict[str, Any]) -> str:
    content = message.get("content")
    return content if isinstance(content, str) else ""


def _is_keep_hint(message: dict[str, Any], keep_sentinels: tuple[str, ...]) -> bool:
    """该 user 消息是否为**注入的纠偏提示 / 反思回填**（属于必保区）。"""
    text = _text_of(message)
    return any(text.startswith(sentinel) for sentinel in keep_sentinels)


def _current_index(messages: list[dict[str, Any]], keep_sentinels: tuple[str, ...]) -> int | None:
    """本轮用户输入的索引：**最后一条非注入提示的 user 消息**。

    循环中追加的纠偏提示同样是 ``role=user``，若简单取「最后一条 user 消息」，一旦本轮
    触发纠偏，真正的用户输入就会被降级成可裁剪的历史——那是本模块最不愿意发生的事。
    """
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") == "user" and not _is_keep_hint(message, keep_sentinels):
            return index
    return None


def classify_zones(messages: list[dict[str, Any]], keep_sentinels: tuple[str, ...]) -> list[str]:
    """逐条消息划区（返回与 ``messages`` 等长的分区名列表）。"""
    current = _current_index(messages, keep_sentinels)
    zones: list[str] = []
    for index, message in enumerate(messages):
        role = message.get("role")
        if role == "system":
            if _text_of(message).startswith(SUMMARY_PREFIX):
                zones.append(BUDGET_ZONE_HISTORY)
            else:
                zones.append(BUDGET_ZONE_SYSTEM if index == 0 else BUDGET_ZONE_RECALL)
            continue
        if role == "tool":
            # 工具回填是**协议必需**：``assistant(tool_calls)`` 之后缺了它，下一轮请求就是
            # 非法历史（部分网关直接报错）；因此无论成败都在必保区
            zones.append(BUDGET_ZONE_KEEP)
            continue
        if role == "user":
            if _is_keep_hint(message, keep_sentinels):
                zones.append(BUDGET_ZONE_KEEP)
            elif index == current:
                zones.append(BUDGET_ZONE_CURRENT)
            else:
                zones.append(BUDGET_ZONE_HISTORY)
            continue
        # assistant：位于本轮用户输入之前的是历史回合；之后的是本轮工作区（协议对，必保）
        zones.append(BUDGET_ZONE_HISTORY if current is None or index < current else BUDGET_ZONE_KEEP)
    return zones


def _turns(messages: list[dict[str, Any]], indices: list[int]) -> list[list[int]]:
    """把历史消息索引按「用户消息开头」切成回合。"""
    turns: list[list[int]] = []
    for index in indices:
        if messages[index].get("role") == "user" or not turns:
            turns.append([index])
        else:
            turns[-1].append(index)
    return turns


def _summary_message(messages: list[dict[str, Any]], turns: list[list[int]], limit: int) -> dict[str, Any]:
    """把若干旧回合折叠成一行摘要（确定性：只做截断与拼接，不做模型改写）。"""
    parts: list[str] = []
    for turn in turns:
        for index in turn:
            text = " ".join(_text_of(messages[index]).split())
            if text:
                parts.append(text)
    joined = " / ".join(parts)
    if len(joined) > limit:
        joined = f"{joined[:limit]}…"
    return {"role": "system", "content": f"{SUMMARY_PREFIX}共 {len(turns)} 轮。{joined}"}


def _usage(
    messages: list[dict[str, Any]], zones: list[str], *, original: Counter | None = None, compressed: bool = False
) -> list[BudgetZoneUsage]:
    chars = Counter()
    counts = Counter()
    for index, message in enumerate(messages):
        chars[zones[index]] += message_chars(message)
        counts[zones[index]] += 1
    usage: list[BudgetZoneUsage] = []
    for zone in BUDGET_ZONES:
        trimmed = 0
        if original is not None:
            trimmed = max(0, original.get(zone, 0) - counts.get(zone, 0))
        usage.append(
            BudgetZoneUsage(
                zone=zone,
                label=BUDGET_ZONE_LABELS.get(zone, zone),
                chars=chars.get(zone, 0),
                messages=counts.get(zone, 0),
                trimmed=trimmed,
                compressed=compressed and zone == BUDGET_ZONE_HISTORY,
            )
        )
    return usage


def _report(
    messages: list[dict[str, Any]],
    zones: list[str],
    *,
    limit: int,
    notes: list[str],
    original: Counter | None = None,
    compressed: bool = False,
) -> BudgetReport:
    used = estimate_chars(messages)
    return BudgetReport(
        limit=limit,
        used=used,
        utilization=round(min(1.0, used / limit), 4) if limit > 0 else 0.0,
        overBudget=limit > 0 and used > limit,
        zones=_usage(messages, zones, original=original, compressed=compressed),
        notes=notes,
    )


def allocate_context(
    messages: list[dict[str, Any]],
    *,
    limit: int,
    keep_sentinels: tuple[str, ...] = (),
    keep_recent_turns: int = BUDGET_KEEP_RECENT_TURNS,
    summary_chars: int = BUDGET_SUMMARY_CHARS,
) -> BudgetReport:
    """按分区预算裁剪 / 压缩 **就地** 修改 ``messages``，并返回分区用量明细。

    ``limit <= 0`` 表示关闭预算管理（沿用「不裁剪」语义），此时只统计用量、不做任何改动。
    """
    limit = int(limit)
    zones = classify_zones(messages, keep_sentinels)
    if limit <= 0:
        return _report(messages, zones, limit=0, notes=[])

    original: Counter = Counter(zones)
    notes: list[str] = []
    compressed = False

    # 1) 检索增强区：按剩余额度截断（固定区不可动，因此先算固定区占用）
    fixed = sum(
        message_chars(messages[index])
        for index, zone in enumerate(zones)
        if zone in (BUDGET_ZONE_SYSTEM, BUDGET_ZONE_KEEP, BUDGET_ZONE_CURRENT)
    )
    flex = max(0, limit - fixed)
    if fixed > limit:
        # 固定区独占预算：此时**弹性区必须清零**（而不是「干脆不裁」）。留着一半历史
        # 与「预算已超」同时成立，只会让超限静默存在且无法收敛；清空弹性区至少保证
        # 除固定区外没有额外浪费，并把原因写进 notes 让人知道该调哪个配置。
        notes.append(
            "固定区（系统提示 + 必保上下文 + 本轮输入）已超出预算，弹性区（检索增强 / 历史回合）已全部清空；"
            "请调大 AGENT_MAX_CONTEXT_CHARS，或收敛本轮任务（减少待确认动作 / 缩短系统提示）。"
        )
    recall_cap = flex // 2
    dropped_recall = 0
    while True:
        zones = classify_zones(messages, keep_sentinels)
        recall_indices = [index for index, zone in enumerate(zones) if zone == BUDGET_ZONE_RECALL]
        recall_chars = sum(message_chars(messages[index]) for index in recall_indices)
        if recall_chars <= recall_cap or not recall_indices:
            break
        # 从靠后的区块开始丢：区块顺序固定为「长期记忆在前、知识库在后」，
        # 记忆（用户偏好 / 决策）通常比一次性检索片段更通用
        del messages[recall_indices[-1]]
        dropped_recall += 1
    if dropped_recall:
        notes.append(f"检索增强区超出额度，已丢弃 {dropped_recall} 个靠后的区块。")

    # 2) 历史回合：先把「最近 N 轮」之外的旧回合折叠成一行摘要（只在确实有额度时才做：
    #    固定区已超预算时压缩出的一行摘要随后也会被丢弃，那只是白做功）
    if flex > 0 and estimate_chars(messages) > limit:
        zones = classify_zones(messages, keep_sentinels)
        turns = _turns(messages, [index for index, zone in enumerate(zones) if zone == BUDGET_ZONE_HISTORY])
        old = turns[:-keep_recent_turns] if keep_recent_turns > 0 else list(turns)
        if old:
            summary = _summary_message(messages, old, summary_chars)
            messages[old[0][0] : old[-1][-1] + 1] = [summary]
            compressed = True
            notes.append(
                f"历史对话超出额度：较早的 {len(old)} 轮已压缩为一行摘要，最近 {min(keep_recent_turns, len(turns))} 轮保留完整内容。"
            )

    # 3) 仍超预算：逐轮丢弃最旧的历史回合（只能丢历史——固定区与必保区是行为契约）
    dropped_turns = 0
    while estimate_chars(messages) > limit:
        zones = classify_zones(messages, keep_sentinels)
        turns = _turns(messages, [index for index, zone in enumerate(zones) if zone == BUDGET_ZONE_HISTORY])
        if not turns:
            notes.append("历史回合已全部丢弃后仍超预算：本轮上下文无法进一步压缩（固定区 + 必保区已接近上限）。")
            break
        turn = turns[0]
        del messages[turn[0] : turn[-1] + 1]
        dropped_turns += 1
    if dropped_turns:
        notes.append(f"压缩后仍超预算，已丢弃最旧的 {dropped_turns} 轮历史对话。")

    if notes:
        METRICS.inc_counter("agent_context_trimmed_total")
    zones = classify_zones(messages, keep_sentinels)
    return _report(messages, zones, limit=limit, notes=notes, original=original, compressed=compressed)


__all__ = [
    "SUMMARY_PREFIX",
    "allocate_context",
    "classify_zones",
    "estimate_chars",
    "message_chars",
]

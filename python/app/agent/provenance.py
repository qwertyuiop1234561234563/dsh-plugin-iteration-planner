"""输出溯源与置信度标注。

问题：模型会写「总工期 42 天、共有 12 个任务、关键路径 5 步」这类**事实性数字**。用户
无从判断它来自引擎实测、知识库检索，还是模型顺口编的——而这三者对决策的可信度完全不同。

本模块把本轮**可回溯的证据**（工具返回值、知识库片段、长期记忆）摊平成**证据原子**，
再把最终结论切句，逐条把「含数字的事实性声明」绑定到来源：

- 有匹配证据 → ``verified``，并给出可点开的引用（工具名 + JSON 字段路径 / chunkId / 记忆 ID）
- 无匹配证据 → ``unverified``（**诚实标注「未核实」，而不是假装它有依据**），并计数

一条刻意的设计边界：匹配口径是**数值可回溯**，不是语义等价。命中「42」只说明某个工具
返回值或检索片段里出现过 42，不构成「该结论与事实等价」的证明。因此本能力只做**降级提示**
（把无据的数字明示出来），不做**事实判定**——后者需要数值语义对齐，属于另一个量级的问题。
把边界写在这里，是为了避免后来者把 ``verified`` 误读成「已证实为真」。
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..constants import (
    MAX_CLAIM_CITATIONS,
    MAX_EVIDENCE_ATOMS,
    MAX_EVIDENCE_VALUE_CHARS,
    PROVENANCE_NUMBER_TOLERANCE,
    PROVENANCE_SOURCE_KB,
    PROVENANCE_SOURCE_MEMORY,
    PROVENANCE_SOURCE_TOOL,
    PROVENANCE_SOURCES,
    PROVENANCE_STATUS_UNVERIFIED,
    PROVENANCE_STATUS_VERIFIED,
)
from ..models import ClaimCitation, MemoryItem, ProvenanceCitation, ProvenanceReport, ToolCallRecord

#: 句子切分（中文标点 + 换行；Markdown 表格行按行切，避免整块表格算一条声明）
_SENTENCE_RE = re.compile(r"[。！？；;!?\n]+")
#: 数字字面量（允许千分位逗号与小数点，兼容百分比）
_NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
#: 单条声明的最长展示长度
_MAX_CLAIM_TEXT = 200


def _to_number(literal: str) -> float | None:
    try:
        return float(literal.replace(",", ""))
    except ValueError:
        return None


def extract_numbers(text: str) -> list[str]:
    """抽取文本中的数字字面量（去重、保持出现顺序）。"""
    seen: list[str] = []
    for match in _NUMBER_RE.finditer(text):
        literal = match.group(0)
        if literal not in seen:
            seen.append(literal)
    return seen


def _numbers_close(left: float, right: float) -> bool:
    """数值是否可视为同一事实（相对容差；``40`` 与 ``40.0`` 必须视为一致）。"""
    if left == right:
        return True
    scale = max(abs(left), abs(right), 1.0)
    return abs(left - right) / scale <= PROVENANCE_NUMBER_TOLERANCE


def _value_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _walk_json(payload: Any, *, path: str, atoms: list[ProvenanceCitation], source: str, depth: int = 0) -> None:
    """递归摊平 JSON：每个叶子值成为一条证据原子（带字段路径，便于人核对）。"""
    if len(atoms) >= MAX_EVIDENCE_ATOMS:
        return
    if isinstance(payload, dict):
        for key, value in payload.items():
            _walk_json(value, path=f"{path}.{key}" if path else str(key), atoms=atoms, source=source, depth=depth + 1)
        return
    if isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            _walk_json(value, path=f"{path}[{index}]", atoms=atoms, source=source, depth=depth + 1)
        return
    # 叶子：只保留**可能承载事实**的标量（``None`` / ``bool`` 无溯源价值，直接跳过）
    if payload is None or isinstance(payload, bool):
        return
    if depth > 8:
        return
    text = _value_text(payload)
    if not text:
        return
    atoms.append(
        ProvenanceCitation(source=source, ref=path or "(root)", label=path or "(root)", value=text[:MAX_EVIDENCE_VALUE_CHARS])
    )


def collect_evidence(
    tool_calls: list[ToolCallRecord] | list[dict[str, Any]],
    references: list[Any] | None = None,
    memories: list[MemoryItem] | list[dict[str, Any]] | None = None,
) -> list[ProvenanceCitation]:
    """把本轮的**可回溯证据**摊平成证据原子列表（工具返回值 / 知识库片段 / 长期记忆）。"""
    atoms: list[ProvenanceCitation] = []
    for call in tool_calls:
        record = call if isinstance(call, ToolCallRecord) else ToolCallRecord.model_validate(call)
        if record.result is None:
            continue
        _walk_json(record.result, path=record.name, atoms=atoms, source=PROVENANCE_SOURCE_TOOL)
        if len(atoms) >= MAX_EVIDENCE_ATOMS:
            break
    for hit in references or []:
        if len(atoms) >= MAX_EVIDENCE_ATOMS:
            break
        chunk_id = getattr(hit, "chunkId", None) or (hit.get("chunkId") if isinstance(hit, dict) else None)
        title = getattr(hit, "title", None) or (hit.get("title") if isinstance(hit, dict) else None)
        text = getattr(hit, "text", None) or (hit.get("text") if isinstance(hit, dict) else "")
        if not text:
            continue
        atoms.append(
            ProvenanceCitation(
                source=PROVENANCE_SOURCE_KB,
                ref=str(chunk_id or title or "kb"),
                label=f"知识库片段{('·' + str(title)) if title else ''}",
                value=str(text)[:MAX_EVIDENCE_VALUE_CHARS],
            )
        )
    for memory in memories or []:
        if len(atoms) >= MAX_EVIDENCE_ATOMS:
            break
        item = memory if isinstance(memory, MemoryItem) else MemoryItem.model_validate(memory)
        content = item.text
        if not content:
            continue
        atoms.append(
            ProvenanceCitation(
                source=PROVENANCE_SOURCE_MEMORY,
                ref=item.memoryId,
                label=f"长期记忆·{item.type}",
                value=str(content)[:MAX_EVIDENCE_VALUE_CHARS],
            )
        )
    return atoms


def _split_claims(content: str) -> list[str]:
    """把结论切成**含数字的事实性声明**（无数字的抒情句不参与溯源）。"""
    claims: list[str] = []
    for chunk in _SENTENCE_RE.split(content or ""):
        text = " ".join(chunk.split())
        if not text or not extract_numbers(text):
            continue
        claims.append(text[:_MAX_CLAIM_TEXT])
    return claims


def analyze_provenance(
    content: str,
    evidence: list[ProvenanceCitation],
) -> ProvenanceReport:
    """逐条声明绑定证据并统计未核实数量（纯函数，可离线断言）。"""
    claims = _split_claims(content)
    prepared: list[tuple[ProvenanceCitation, list[float]]] = []
    for atom in evidence:
        numbers = [value for value in (_to_number(item) for item in extract_numbers(atom.value)) if value is not None]
        if numbers:
            prepared.append((atom, numbers))

    citations: list[ClaimCitation] = []
    for index, text in enumerate(claims, start=1):
        literals = extract_numbers(text)
        matched: list[ProvenanceCitation] = []
        for literal in literals:
            value = _to_number(literal)
            if value is None:
                continue
            for atom, numbers in prepared:
                if any(_numbers_close(value, candidate) for candidate in numbers) and atom not in matched:
                    matched.append(atom)
                    break
            if len(matched) >= MAX_CLAIM_CITATIONS:
                break
        citations.append(
            ClaimCitation(
                index=index,
                text=text,
                status=PROVENANCE_STATUS_VERIFIED if matched else PROVENANCE_STATUS_UNVERIFIED,
                numbers=literals,
                citations=matched,
                detail="命中工具返回值 / 知识库片段 / 长期记忆中的数值" if matched else "结论中的数字在本轮证据里找不到来源",
            )
        )

    sources = {source: sum(1 for atom in evidence if atom.source == source) for source in PROVENANCE_SOURCES}
    verified = sum(1 for claim in citations if claim.status == PROVENANCE_STATUS_VERIFIED)
    total = len(citations)
    return ProvenanceReport(
        totalClaims=total,
        verified=verified,
        unverified=total - verified,
        confidence=round(verified / total, 4) if total else 1.0,
        evidenceCount=len(evidence),
        sources=sources,
        claims=citations,
    )


__all__ = ["analyze_provenance", "collect_evidence", "extract_numbers"]

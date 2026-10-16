"""文档分块（递归字符分块，Recursive Character Splitting）。

主流做法：按「结构边界优先级」递归下钻切分——先按 Markdown 标题，再按空行段落，
再按中文句读 / 英文句点，最后按字符硬切；随后**贪心合并**到目标长度并保留重叠，
兼顾「语义完整」与「块大小可控」。

不引入 ``langchain`` 等重依赖：分块逻辑本身不足百行，自实现可避免为单一功能引入
庞大的传递依赖（与项目「最小依赖」取向一致）。
"""

from __future__ import annotations

#: 分隔符优先级（由粗到细）；末尾的空串表示「按字符硬切」，是递归的兜底
_SEPARATORS: tuple[str, ...] = (
    "\n## ",
    "\n### ",
    "\n#### ",
    "\n\n",
    "\n",
    "。",
    "；",
    "！",
    "？",
    ". ",
    "，",
    " ",
    "",
)


def chunk_text(text: str, *, size: int = 800, overlap: int = 120) -> list[str]:
    """把长文本切成若干净片段。

    :param text: 原始文本。
    :param size: 目标块长度（字符）；实际块长允许在合并阶段略超。
    :param overlap: 相邻块的重叠字符数（保留上下文连续性）。
    """
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    resolved_size = max(50, int(size))
    resolved_overlap = max(0, min(int(overlap), resolved_size // 2))
    pieces = _split_recursive(normalized, _SEPARATORS, resolved_size)
    return _merge(pieces, resolved_size, resolved_overlap)


# ------------------------------- 内部实现 -------------------------------


def _split_recursive(text: str, separators: tuple[str, ...], size: int) -> list[str]:
    """按分隔符优先级递归下钻，直到每片都不超过 ``size``。"""
    if len(text) <= size:
        return [text]
    if not separators:
        # 兜底：按固定长度硬切
        return [text[index : index + size] for index in range(0, len(text), size)]

    separator = separators[0]
    if separator == "":
        return [text[index : index + size] for index in range(0, len(text), size)]

    parts = text.split(separator)
    # 分隔符回挂到前一片，保持标题 / 句读不丢失
    pieces = [part + separator for part in parts[:-1]] + [parts[-1]]
    if len(pieces) == 1:
        # 该分隔符未命中，继续用更细的分隔符
        return _split_recursive(text, separators[1:], size)

    result: list[str] = []
    for piece in pieces:
        if not piece:
            continue
        if len(piece) <= size:
            result.append(piece)
        else:
            result.extend(_split_recursive(piece, separators[1:], size))
    return result


def _merge(pieces: list[str], size: int, overlap: int) -> list[str]:
    """贪心合并小片到目标长度，块间保留 ``overlap`` 字符重叠。"""
    chunks: list[str] = []
    buffer = ""
    for piece in pieces:
        if buffer and len(buffer) + len(piece) > size:
            chunks.append(buffer.strip())
            tail = buffer[-overlap:] if overlap > 0 else ""
            # 重叠尾巴加进下一块；若尾巴本身已接近上限则从零开始，避免块内重复膨胀
            buffer = (tail + piece) if len(tail) + len(piece) <= size + overlap else piece
        else:
            buffer += piece
    if buffer.strip():
        chunks.append(buffer.strip())
    return [chunk for chunk in chunks if chunk]


__all__ = ["chunk_text"]

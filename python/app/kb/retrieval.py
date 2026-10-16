"""混合检索（Hybrid Retrieval）：稠密向量 + BM25 稀疏 → RRF 融合。

为什么混合：
- **稠密**（embedding）擅长语义近似，但对专有名词、编号、缩写等「字面精确匹配」偏弱；
- **稀疏**（BM25）擅长字面命中，但对同义改写不敏感；
- 两者互补后用 **RRF（Reciprocal Rank Fusion）** 融合，无需调参即可稳定优于单路，
  是当前 RAG 检索的主流工程实践（``score = Σ 1/(k + rank)``，k 经验取 60）。

中文分词说明：不引入 ``jieba`` 等额外依赖，采用**字符级 unigram + bigram** 切分——
对中文检索效果接近分词（bigram 已能刻画双字词），且零依赖、可复现。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

#: 中日韩统一表意文字（含扩展 A）连续串
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
#: 拉丁字母 / 数字 / 下划线串（英文单词、编号、版本号等）
_ASCII_WORD = re.compile(r"[A-Za-z0-9_]+")


def tokenize(text: str) -> list[str]:
    """把文本切分为检索词元（中文 unigram+bigram，英文数字按词）。

    中文连续串同时产出单字与相邻双字（bigram），兼顾单字召回与双字词精度；
    英文 / 数字 / 下划线串按整词切分并小写化。
    """
    if not text:
        return []
    tokens: list[str] = []
    for match in _CJK_RUN.finditer(text):
        run = match.group()
        tokens.extend(run)
        tokens.extend(run[index] + run[index + 1] for index in range(len(run) - 1))
    tokens.extend(word.lower() for word in _ASCII_WORD.findall(text))
    return tokens


def bm25_search(
    rows: list[dict[str, Any]],
    query: str,
    *,
    top_k: int,
    k1: float = 1.5,
    b: float = 0.75,
) -> list[dict[str, Any]]:
    """在给定分块集合上做 BM25 打分检索。

    :param rows: 分块集合，元素为 ``{"id", "document", "metadata"}``。
    :param query: 查询文本。
    :param top_k: 返回条数。
    :return: 按 BM25 得分降序的 ``[{id, document, metadata, score}]``。
    """
    query_tokens = tokenize(query)
    if not query_tokens or not rows:
        return []

    doc_tokens = [tokenize(str(row.get("document") or "")) for row in rows]
    lengths = [len(tokens) for tokens in doc_tokens]
    total = len(rows)
    avg_length = sum(lengths) / total if total else 0.0

    # 文档频率（DF）：某个词元出现在多少个分块中
    doc_freq: Counter[str] = Counter()
    for tokens in doc_tokens:
        doc_freq.update(set(tokens))

    scored: list[dict[str, Any]] = []
    for row, tokens, length in zip(rows, doc_tokens, lengths):
        frequencies = Counter(tokens)
        score = 0.0
        for term in query_tokens:
            frequency = frequencies.get(term, 0)
            if frequency == 0:
                continue
            df = doc_freq.get(term, 0)
            idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
            denominator = frequency + k1 * (1 - b + b * (length / avg_length if avg_length else 1))
            score += idf * (frequency * (k1 + 1)) / denominator
        if score > 0:
            scored.append(
                {
                    "id": row.get("id"),
                    "document": row.get("document"),
                    "metadata": row.get("metadata") or {},
                    "score": round(score, 6),
                }
            )
    scored.sort(key=lambda item: item["score"], reverse=True)
    return scored[:top_k]


def rrf_fuse(rankings: list[list[str]], *, k: int = 60, weights: list[float] | None = None) -> dict[str, float]:
    """RRF 融合多路排序（输入为按名次排列的 id 列表）。

    ``score(id) = Σ_i weight_i / (k + rank_i(id))``；未出现在某一路中则不贡献该项。
    """
    fused: dict[str, float] = {}
    for index, ranking in enumerate(rankings):
        weight = 1.0 if not weights else (weights[index] if index < len(weights) else 1.0)
        for rank, item_id in enumerate(ranking, start=1):
            fused[item_id] = fused.get(item_id, 0.0) + weight / (k + rank)
    return fused


__all__ = ["tokenize", "bm25_search", "rrf_fuse"]

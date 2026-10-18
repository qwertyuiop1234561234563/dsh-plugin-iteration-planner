"""模型输出 → JSON 解析（容错）。

多数 OpenAI 兼容网关在 ``response_format=json_object`` 之外仍会返回围栏或前后缀说明，
因此解析必须是**容错**的：先剥 Markdown 围栏，再取首个 JSON 对象。解析失败返回 ``None``，
由调用方决定降级策略（绝不猜、不截断补全成「看似合法」的结构）。
"""

from __future__ import annotations

import json
import re
from typing import Any

#: 首个 JSON 对象（跨行）
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)
_FENCE_HEAD = re.compile(r"^```[a-zA-Z0-9_-]*\s*")
_FENCE_TAIL = re.compile(r"\s*```$")


def extract_json_object(text: str) -> dict[str, Any] | None:
    """从模型文本中提取首个 JSON 对象；无法解析时返回 ``None``。"""
    if not text:
        return None
    cleaned = _FENCE_TAIL.sub("", _FENCE_HEAD.sub("", text.strip()))
    match = _JSON_OBJECT.search(cleaned)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


__all__ = ["extract_json_object"]

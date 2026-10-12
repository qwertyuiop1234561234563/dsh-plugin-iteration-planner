"""Python 对外 API 的请求 / 响应模型。

仅覆盖 Python 原生端点（``/api/agent/*``）；``/api/plans/**`` 为透明转发，
校验与信封由 TypeScript 引擎统一负责，避免两处校验规则漂移。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

_ISO_DATE = r"^\d{4}-\d{2}-\d{2}$"


class AgentPlanRequest(BaseModel):
    """自然语言需求 → 完整迭代计划（Agent 端到端编排）。"""

    requirement: str = Field(min_length=1, max_length=20_000, description="业务需求原文")
    iterationDeadline: str | None = Field(default=None, pattern=_ISO_DATE, description="迭代截止日期 YYYY-MM-DD")
    iterationStartDate: str | None = Field(default=None, pattern=_ISO_DATE, description="迭代起始日期 YYYY-MM-DD")
    devMemberCount: int | None = Field(default=None, gt=0, le=1000, description="可用开发人数")


class AgentChatRequest(BaseModel):
    """会话式对话（携带上下文，可多轮修改计划）。"""

    message: str = Field(min_length=1, max_length=20_000, description="用户消息")
    sessionId: str | None = Field(default=None, max_length=128, description="会话 ID；缺省则新建会话")


class ToolCallRecord(BaseModel):
    """单次工具调用记录（用于前端展示与审计）。"""

    name: str
    arguments: dict[str, Any]
    ok: bool
    result: Any | None = None
    error: str | None = None


class AgentRunResult(BaseModel):
    """Agent 一次运行的结构化输出。"""

    content: str
    planId: str | None = None
    toolCalls: list[ToolCallRecord] = Field(default_factory=list)
    steps: int = 0


class AgentChatResponse(AgentRunResult):
    """会话式对话响应（附带会话 ID）。"""

    sessionId: str
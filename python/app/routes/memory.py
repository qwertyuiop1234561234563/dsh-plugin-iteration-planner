"""长期记忆端点（``/api/memories/**``）。

记忆由 Agent 在每轮对话后**自动提取**（LLM 抽取 → 向量去重 → 入库），本组端点提供
人工治理与检视能力：

- ``GET    /memories``：清单（可按状态过滤）；
- ``POST   /memories``：手工新增（用于把明确约定直接固化，无需等待对话触发）；
- ``PATCH  /memories/{id}``：启用 / 停用（停用后保留数据但不再参与语义召回）；
- ``DELETE /memories/{id}``：删除单条；
- ``DELETE /memories``：清空全部（危险操作，前端需二次确认）；
- ``POST   /memories/search``：按语义召回试跑（查看「模型当前会想起什么」）；
- ``GET    /memories/stats``：统计。 

未配置向量化模型时统一返回 ``503 EMBEDDING_UNAVAILABLE``。
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from ..agent.memory import MemoryService
from ..errors import embedding_unavailable
from ..models import (
    MemoryCreateRequest,
    MemoryItem,
    MemoryList,
    MemorySearchRequest,
    MemorySearchResult,
    MemoryStats,
    MemoryStatusRequest,
)
from ..state import AppContext


def _require_memory(ctx: AppContext) -> MemoryService:
    if ctx.memory is None:
        raise embedding_unavailable()
    return ctx.memory


def build_memory_router(base: str) -> APIRouter:
    router = APIRouter(prefix=f"{base}/memories", tags=["memory"])

    @router.get("/stats", summary="长期记忆统计")
    async def memory_stats(request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        stats = await _require_memory(ctx).stats()
        return {"data": MemoryStats(**stats).model_dump()}

    @router.get("", summary="长期记忆清单")
    async def list_memories(
        request: Request,
        status: str | None = Query(default=None, description="active | disabled，缺省返回全部"),
        limit: int = Query(default=500, ge=1, le=2000, description="返回条数上限"),
    ) -> dict:
        ctx: AppContext = request.app.state.ctx
        items = await _require_memory(ctx).list_memories(status=status, limit=limit)
        return {"data": MemoryList(items=items, total=len(items)).model_dump()}

    @router.post("", summary="手工新增一条长期记忆")
    async def create_memory(payload: MemoryCreateRequest, request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        memory = await _require_memory(ctx).add_memory(
            payload.text,
            memory_type=payload.type,
            importance=payload.importance,
            tags=payload.tags,
            source=payload.source or "manual",
        )
        return {"data": MemoryItem(**memory).model_dump()}

    @router.post("/search", summary="按语义召回记忆（试跑）")
    async def search_memory(payload: MemorySearchRequest, request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        items = await _require_memory(ctx).recall(payload.query, top_k=payload.topK)
        return {"data": MemorySearchResult(query=payload.query, items=items).model_dump()}

    @router.patch("/{memory_id}", summary="启用 / 停用一条记忆")
    async def update_memory(memory_id: str, payload: MemoryStatusRequest, request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        memory = await _require_memory(ctx).set_status(memory_id, payload.status)
        return {"data": MemoryItem(**memory).model_dump()}

    @router.delete("/{memory_id}", summary="删除一条记忆")
    async def delete_memory(memory_id: str, request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        result = await _require_memory(ctx).delete_memory(memory_id)
        return {"data": result}

    @router.delete("", summary="清空全部长期记忆")
    async def clear_memories(request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        result = await _require_memory(ctx).clear_all()
        return {"data": result}

    return router


__all__ = ["build_memory_router"]

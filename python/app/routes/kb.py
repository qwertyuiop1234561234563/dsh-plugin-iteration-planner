"""知识库端点（``/api/kb/**``）。

能力（完整 RAG 链路的人工入口）：

- ``POST /kb/documents``：**粘贴文本**建库（表单式录入）；
- ``POST /kb/documents/upload``：**上传文件**建库（md/txt/csv/json/html/pdf/docx）；
- ``GET  /kb/documents`` / ``GET /kb/documents/{id}``：清单与详情（含分块预览）；
- ``DELETE /kb/documents/{id}``：删除文档及其全部分块；
- ``POST /kb/search``：混合检索试跑（查看命中了哪些片段，便于调参 / 校验资料质量）；
- ``GET  /kb/stats``：统计与检索配置。

未配置向量化模型（缺少 ``EMBEDDING_API_KEY`` / 向量端点不可用）时统一返回
``503 EMBEDDING_UNAVAILABLE``，并按当前配置给出「待补齐项」指引，而不是静默返回空结果
（避免用户误判为「知识库里没有」）。
"""

from __future__ import annotations

from fastapi import APIRouter, File, Form, Request, UploadFile

from ..constants import KB_DOCUMENT_EXTENSIONS
from ..errors import bad_request, embedding_unavailable
from ..kb.pipeline import KnowledgeBaseService
from ..models import (
    KbDeleteResult,
    KbDocumentCreateRequest,
    KbDocumentDetail,
    KbDocumentList,
    KbHitList,
    KbSearchRequest,
    KbStats,
)
from ..state import AppContext

#: 上传表单字段的长度上限（multipart 字段不走 pydantic，需手工截断防脏数据）
_MAX_TITLE_CHARS = 200
_MAX_SOURCE_CHARS = 200
_MAX_TAGS = 10


def _require_knowledge(ctx: AppContext) -> KnowledgeBaseService:
    if ctx.knowledge is None:
        raise embedding_unavailable(hint=ctx.settings.embedding_setup_hint)
    return ctx.knowledge


def _parse_tags(raw: str) -> list[str]:
    """解析上传表单里的标签（英文逗号或中文逗号分隔）。"""
    items = [item.strip() for item in raw.replace("，", ",").split(",")]
    return [item for item in items if item][:_MAX_TAGS]


def build_kb_router(base: str) -> APIRouter:
    router = APIRouter(prefix=f"{base}/kb", tags=["knowledge"])

    @router.get("/stats", summary="知识库统计与检索配置")
    async def kb_stats(request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        stats = await _require_knowledge(ctx).stats()
        return {"data": KbStats(**stats).model_dump()}

    @router.get("/documents", summary="知识库文档清单")
    async def list_documents(request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        documents = await _require_knowledge(ctx).list_documents()
        return {"data": KbDocumentList(items=documents, total=len(documents)).model_dump()}

    @router.post("/documents", summary="以粘贴文本的方式新建文档")
    async def create_document(payload: KbDocumentCreateRequest, request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        knowledge = _require_knowledge(ctx)
        # 复用统一的解析 / 分块链路：把正文包装为 Markdown 文档，扩展名后缀便于元数据展示
        document = await knowledge.ingest(
            f"{payload.title.strip()}.md",
            payload.content.encode("utf-8"),
            title=payload.title.strip(),
            source=(payload.source or "").strip(),
            tags=payload.tags,
        )
        return {"data": document}

    @router.post("/documents/upload", summary=f"上传文件建库（支持 {' / '.join(KB_DOCUMENT_EXTENSIONS)}）")
    async def upload_document(
        request: Request,
        file: UploadFile = File(..., description="待入库文件"),
        title: str = Form(default="", description="文档标题（缺省取文件名）"),
        source: str = Form(default="", description="来源说明"),
        tags: str = Form(default="", description="标签，逗号分隔"),
    ) -> dict:
        ctx: AppContext = request.app.state.ctx
        knowledge = _require_knowledge(ctx)
        filename = (file.filename or "").strip()
        if not filename:
            raise bad_request("上传文件缺少文件名，无法判定解析格式")
        data = await file.read()
        if not data:
            raise bad_request("上传文件内容为空")
        document = await knowledge.ingest(
            filename,
            data,
            title=title.strip()[:_MAX_TITLE_CHARS] or None,
            source=source.strip()[:_MAX_SOURCE_CHARS],
            tags=_parse_tags(tags),
        )
        return {"data": document}

    @router.get("/documents/{doc_id}", summary="文档详情（含分块预览）")
    async def get_document(doc_id: str, request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        detail = await _require_knowledge(ctx).get_document(doc_id)
        return {"data": KbDocumentDetail(**detail).model_dump()}

    @router.delete("/documents/{doc_id}", summary="删除文档及其全部分块")
    async def delete_document(doc_id: str, request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        result = await _require_knowledge(ctx).delete_document(doc_id)
        return {"data": KbDeleteResult(**result).model_dump()}

    @router.post("/search", summary="混合检索（稠密向量 + BM25 → RRF）")
    async def search(payload: KbSearchRequest, request: Request) -> dict:
        ctx: AppContext = request.app.state.ctx
        knowledge = _require_knowledge(ctx)
        hits = await knowledge.search(payload.query, top_k=payload.topK, hybrid=payload.hybrid)
        mode = "hybrid" if (payload.hybrid if payload.hybrid is not None else knowledge.hybrid_enabled) else "dense"
        return {"data": KbHitList(query=payload.query, mode=mode, items=hits).model_dump()}

    return router


__all__ = ["build_kb_router"]

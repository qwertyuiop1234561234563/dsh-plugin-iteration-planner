"""知识库（RAG）测试：分块、解析、混合检索、向量库读写与 REST 契约。

全部使用**桩向量化器 + 临时向量库**，不触网、不依赖外部服务；断言覆盖
「正常路径 / 空态 / 错误码 / 降级」，而非仅验证返回 200。
"""

from __future__ import annotations

import io
import json

import httpx
import pytest

from app.errors import ApiError
from app.constants import KB_COLLECTION
from app.kb.chunking import chunk_text
from app.kb.loader import load_document
from app.kb.retrieval import bm25_search, rrf_fuse, tokenize
from tests.conftest import FailingEmbedder, auth


# ============================ 分块 ============================


def test_chunk_text_returns_empty_for_blank():
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []


def test_chunk_text_splits_long_text_within_budget():
    text = "\n\n".join(f"第{i}段：这是用于验证分块的段落内容。" * 6 for i in range(24))
    chunks = chunk_text(text, size=300, overlap=50)
    assert len(chunks) > 1
    # 合并阶段允许轻微超长（重叠尾巴），但不应出现「远超目标」的块
    assert all(len(chunk) <= 300 + 50 for chunk in chunks)
    assert all(chunk.strip() for chunk in chunks)


def test_chunk_text_preserves_content_after_merge():
    text = "\n".join(f"行{i}" for i in range(400))
    chunks = chunk_text(text, size=200, overlap=20)
    joined = "".join(chunks)
    # 首尾行必须被保留（避免切分丢内容）
    assert "行0" in joined and "行399" in joined


def test_chunk_text_small_text_single_chunk():
    assert chunk_text("很短的一段话", size=800, overlap=120) == ["很短的一段话"]


# ============================ 检索基础件 ============================


def test_tokenize_chinese_bigram_and_ascii_words():
    tokens = tokenize("Kahn 拓扑排序 TOPO-1")
    assert "kahn" in tokens
    assert "拓扑" in tokens and "扑排" in tokens  # 中文 bigram
    assert "topo" in tokens and "1" in tokens


def test_bm25_ranks_literal_match_first():
    rows = [
        {"id": "1", "document": "关键路径由 AOE 算法计算得出", "metadata": {}},
        {"id": "2", "document": "人员过载风险需要削峰处理", "metadata": {}},
    ]
    hits = bm25_search(rows, "关键路径", top_k=2)
    assert hits and hits[0]["id"] == "1"
    assert hits[0]["score"] > 0


def test_bm25_returns_empty_for_unmatched_query():
    rows = [{"id": "1", "document": "完全无关的内容", "metadata": {}}]
    assert bm25_search(rows, "量子力学", top_k=3) == []


def test_rrf_fuse_ranks_documents_hit_by_both_channels_higher():
    fused = rrf_fuse([["a", "b"], ["b", "c"]], k=60)
    assert fused["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert fused["b"] > fused["a"] > fused["c"] - 1


# ============================ 文档解析 ============================


def test_load_markdown_document():
    loaded = load_document("需求.md", "# 标题\n\n正文内容".encode())
    assert "正文内容" in loaded.text
    assert loaded.meta["extension"] == ".md"
    assert loaded.meta["charCount"] > 0


def test_load_json_document_is_flattened():
    payload = {"plan": {"tasks": [{"name": "数据库设计"}]}}
    loaded = load_document("plan.json", json.dumps(payload, ensure_ascii=False).encode())
    assert "plan.tasks[0].name: 数据库设计" in loaded.text


def test_load_html_document_strips_tags_and_scripts():
    html = "<html><head><style>.a{color:red}</style></head><body><h1>标题</h1><p>正文&nbsp;内容</p></body></html>"
    loaded = load_document("page.html", html.encode())
    assert "标题" in loaded.text
    assert "正文 内容" in loaded.text
    assert "<h1>" not in loaded.text and "color:red" not in loaded.text


def test_load_unknown_binary_extension_fails_clearly():
    with pytest.raises(ApiError) as exc:
        load_document("blob.bin", bytes([0x00, 0xFF, 0xFE, 0x81, 0x00]))
    assert exc.value.status_code == 400
    assert "支持的格式" in exc.value.message


def test_load_empty_document_fails():
    with pytest.raises(ApiError) as exc:
        load_document("empty.txt", b"")
    assert "内容为空" in exc.value.message


def test_load_whitespace_document_fails():
    with pytest.raises(ApiError) as exc:
        load_document("blank.md", b"   \n\n \t ")
    assert "未解析出可用文本" in exc.value.message


# ============================ 向量库直连 ============================


@pytest.mark.asyncio
async def test_store_upsert_query_delete_roundtrip(kb_store):
    kb_store.upsert(
        "chunks",
        ids=["d:0000", "e:0000"],
        documents=["关键路径计算", "人员过载削峰"],
        embeddings=[[1.0, 0.0], [0.0, 1.0]],
        metadatas=[{"docId": "d", "chunkIndex": 0}, {"docId": "e", "chunkIndex": 0}],
    )
    assert kb_store.count("chunks") == 2
    # 同时支持「别名」与「集合全名」两种寻址方式（调用方分别使用二者）
    assert kb_store.count(KB_COLLECTION) == 2
    rows = kb_store.query("chunks", embedding=[1.0, 0.1], top_k=2)
    assert rows[0]["id"] == "d:0000"
    assert rows[0]["score"] > rows[1]["score"]
    assert kb_store.get_by_ids("chunks", ids=["e:0000"])[0]["metadata"]["docId"] == "e"
    kb_store.update_metadata("chunks", ids=["e:0000"], metadatas=[{"docId": "e", "chunkIndex": 0, "status": "disabled"}])
    assert kb_store.get_by_ids("chunks", ids=["e:0000"], include_documents=False)[0]["metadata"]["status"] == "disabled"
    kb_store.delete("chunks", where={"docId": "d"})
    assert kb_store.count("chunks") == 1
    kb_store.reset("chunks")
    assert kb_store.count("chunks") == 0


def test_store_rejects_unknown_collection(kb_store):
    with pytest.raises(ApiError):
        kb_store.count("unknown")


def test_store_reset_by_full_name_keeps_handle_usable(kb_store):
    """回归：以**集合全名**清空后，缓存句柄必须换成新集合，否则后续读写会报「集合不存在」。

    自愈逻辑重建集合时传入的是 ``KB_COLLECTION``（全名）而非别名，此处即守护该路径。
    """
    kb_store.upsert(
        "chunks",
        ids=["d:0000"],
        documents=["关键路径计算"],
        embeddings=[[1.0, 0.0]],
        metadatas=[{"docId": "d", "chunkIndex": 0}],
    )
    kb_store.reset(KB_COLLECTION)
    assert kb_store.count(KB_COLLECTION) == 0

    kb_store.upsert(
        KB_COLLECTION,
        ids=["d:0000"],
        documents=["关键路径计算"],
        embeddings=[[1.0, 0.0]],
        metadatas=[{"docId": "d", "chunkIndex": 0}],
    )
    assert kb_store.count("chunks") == 1
    assert kb_store.query("chunks", embedding=[1.0, 0.1], top_k=1)[0]["id"] == "d:0000"


# ============================ 服务层 ============================


@pytest.mark.asyncio
async def test_service_ingest_search_list_delete(kb_service, kb_store):
    document = await kb_service.ingest(
        "迭代规范.md",
        "# 规范\n\n关键路径必须由 AOE 算法计算，禁止手工排期。".encode(),
        title="迭代规范",
        source="wiki",
        tags=["规范", "排期"],
    )
    assert document["docId"] and document["chunkCount"] >= 1
    assert document["tags"] == ["规范", "排期"]

    hits = await kb_service.search("关键路径怎么算")
    assert hits and hits[0]["docId"] == document["docId"]
    assert "AOE" in hits[0]["text"]
    assert hits[0]["title"] == "迭代规范"

    documents = await kb_service.list_documents()
    assert len(documents) == 1 and documents[0]["chunkCount"] == document["chunkCount"]

    detail = await kb_service.get_document(document["docId"])
    assert len(detail["chunks"]) == document["chunkCount"]
    assert detail["chunks"][0]["chunkIndex"] == 0

    stats = await kb_service.stats()
    assert stats["documentCount"] == 1 and stats["chunkCount"] == document["chunkCount"]
    assert stats["totalChars"] > 0 and stats["embedder"] == "stub-embedder"

    deleted = await kb_service.delete_document(document["docId"])
    assert deleted["deletedChunks"] == document["chunkCount"]
    assert await kb_service.list_documents() == []


@pytest.mark.asyncio
async def test_service_search_requires_query(kb_service):
    with pytest.raises(ApiError):
        await kb_service.search("   ")


@pytest.mark.asyncio
async def test_service_search_on_empty_store_returns_empty(kb_service):
    assert await kb_service.search("任意问题") == []


@pytest.mark.asyncio
async def test_service_get_and_delete_missing_document(kb_service):
    with pytest.raises(ApiError) as exc:
        await kb_service.get_document("missing")
    assert exc.value.status_code == 404
    with pytest.raises(ApiError):
        await kb_service.delete_document("missing")


@pytest.mark.asyncio
async def test_service_dense_mode_without_hybrid(kb_store, stub_embedder):
    from app.kb.pipeline import build_knowledge_base

    service = build_knowledge_base(kb_store, stub_embedder, hybrid=False)
    await service.ingest("规范.md", "关键路径由 AOE 算法计算。".encode())
    hits = await service.search("AOE 关键路径")
    assert hits and hits[0]["docId"]


@pytest.mark.asyncio
async def test_service_context_and_hits_downgrades_on_embedder_failure(kb_store):
    from app.kb.pipeline import build_knowledge_base

    service = build_knowledge_base(kb_store, FailingEmbedder())
    context, hits = await service.context_and_hits("任意问题")
    assert context is None and hits == []


@pytest.mark.asyncio
async def test_service_rejects_oversized_document(kb_store, stub_embedder):
    from app.kb.pipeline import build_knowledge_base

    service = build_knowledge_base(kb_store, stub_embedder, max_document_bytes=10)
    with pytest.raises(ApiError) as exc:
        await service.ingest("big.md", b"x" * 50)
    assert "体积超限" in exc.value.message


# ============================ REST 契约 ============================


def test_kb_endpoints_disabled_without_embedder(client):
    """未配置向量化模型时，知识库端点必须明确返回 503 而不是空列表。

    且文案必须指向**真实待补齐项**（``EMBEDDING_API_KEY``）——写死「LLM_API_KEY」会把用户
    引向一个必然失败的配置（拿 A 家密钥请求 B 家端点），前端据此渲染的配置引导也会误导。
    """
    response = client.get("/api/kb/documents", headers=auth())
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "EMBEDDING_UNAVAILABLE"
    assert "EMBEDDING_API_KEY" in body["message"]


def test_kb_health_flags(kb_client):
    with kb_client() as c:
        health = c.get("/api/agent/health", headers=auth()).json()["data"]
        assert health["knowledgeEnabled"] is True
        assert health["memoryEnabled"] is True
        assert "kb_search" in health["tools"]


def test_kb_full_rest_flow(kb_client):
    with kb_client() as c:
        headers = auth()
        created = c.post(
            "/api/kb/documents",
            json={"title": "发布流程", "content": "# 发布\n\n灰度发布必须先内测再全量。", "tags": ["发布"]},
            headers=headers,
        )
        assert created.status_code == 200
        doc_id = created.json()["data"]["docId"]

        listing = c.get("/api/kb/documents", headers=headers).json()["data"]
        assert listing["total"] == 1

        uploaded = c.post(
            "/api/kb/documents/upload",
            files={"file": ("规范.txt", io.BytesIO("关键路径由 AOE 算法计算。".encode()), "text/plain")},
            data={"title": "算法规范", "tags": "算法,排期"},
            headers=headers,
        )
        assert uploaded.status_code == 200
        assert uploaded.json()["data"]["tags"] == ["算法", "排期"]

        search = c.post("/api/kb/search", json={"query": "灰度发布", "topK": 3}, headers=headers)
        assert search.status_code == 200
        body = search.json()["data"]
        assert body["mode"] == "hybrid" and body["items"]
        assert body["items"][0]["docId"] == doc_id

        detail = c.get(f"/api/kb/documents/{doc_id}", headers=headers)
        assert detail.status_code == 200
        assert detail.json()["data"]["chunks"]

        stats = c.get("/api/kb/stats", headers=headers).json()["data"]
        assert stats["documentCount"] == 2 and stats["hybrid"] is True

        removed = c.delete(f"/api/kb/documents/{doc_id}", headers=headers)
        assert removed.status_code == 200 and removed.json()["data"]["deleted"] is True
        assert c.get(f"/api/kb/documents/{doc_id}", headers=headers).status_code == 404


def test_kb_search_validation(kb_client):
    with kb_client() as c:
        assert c.post("/api/kb/search", json={"query": "  "}, headers=auth()).status_code == 400
        # 项目统一把 pydantic 校验失败映射为 400（见 main.py 的异常处理器）
        assert c.post("/api/kb/search", json={"query": "x", "topK": 0}, headers=auth()).status_code == 400


def test_kb_upload_rejects_empty_file(kb_client):
    with kb_client() as c:
        response = c.post(
            "/api/kb/documents/upload",
            files={"file": ("empty.txt", io.BytesIO(b""), "text/plain")},
            headers=auth(),
        )
        assert response.status_code == 400


def test_kb_dense_mode_endpoint(kb_client):
    with kb_client(overrides={"kb_hybrid": False}) as c:
        c.post("/api/kb/documents", json={"title": "t", "content": "关键路径计算"}, headers=auth())
        body = c.post("/api/kb/search", json={"query": "关键路径"}, headers=auth()).json()["data"]
        assert body["mode"] == "dense" and body["items"]


# ============================ 辅助 ============================


@pytest.fixture
def kb_service(kb_store, stub_embedder):
    from app.kb.pipeline import build_knowledge_base

    return build_knowledge_base(kb_store, stub_embedder, chunk_size=200, chunk_overlap=40)


# ================== 向量化请求体：dimensions 按模型能力下发 ==================


def _embedding_handler(seen: dict):
    """记录请求体，并按输入条数回填同序向量（8 维）。"""

    def handler(_request):
        seen.update(json.loads(_request.content))
        count = len(seen.get("input") or [])
        return httpx.Response(200, json={"data": [{"index": i, "embedding": [0.5] * 8} for i in range(count)]})

    return handler


async def test_embedder_omits_dimensions_for_fixed_dim_models():
    """固定维度模型（默认的 BAAI/bge-m3）不得下发 dimensions 字段：上游不认该参数会直接 400。"""
    from app.kb.embedder import OpenAiEmbedder

    seen: dict = {}
    embedder = OpenAiEmbedder(
        "http://stub.test/v1",
        "stub-key",
        "BAAI/bge-m3",
        dimensions=8,
        send_dimensions=False,
        transport=httpx.MockTransport(_embedding_handler(seen)),
    )
    vectors = await embedder.embed(["灰度发布", "关键路径"])
    await embedder.aclose()

    assert len(vectors) == 2 and len(vectors[0]) == 8
    assert "dimensions" not in seen


async def test_embedder_sends_dimensions_when_model_supports_it():
    """支持自定义维度的模型（智谱 embedding-3 等）必须显式下发 dimensions，否则会拿到默认维度。"""
    from app.kb.embedder import OpenAiEmbedder

    seen: dict = {}
    embedder = OpenAiEmbedder(
        "http://stub.test/v1",
        "stub-key",
        "embedding-3",
        dimensions=8,
        send_dimensions=True,
        transport=httpx.MockTransport(_embedding_handler(seen)),
    )
    await embedder.embed(["灰度发布"])
    await embedder.aclose()

    assert seen["dimensions"] == 8


async def test_embedder_dimension_mismatch_hint_is_actionable():
    """维度与配置不符时显式失败，并给出可操作提示（区分固定维度 / 可自定义维度）。"""
    from app.kb.embedder import OpenAiEmbedder

    def handler(_request):
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.5] * 4}]})

    embedder = OpenAiEmbedder(
        "http://stub.test/v1",
        "stub-key",
        "BAAI/bge-m3",
        dimensions=8,
        send_dimensions=False,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ApiError) as exc:
        await embedder.embed(["x"])
    await embedder.aclose()

    assert "向量维度与配置不一致" in exc.value.message
    assert "EMBEDDING_SEND_DIMENSIONS" in exc.value.message

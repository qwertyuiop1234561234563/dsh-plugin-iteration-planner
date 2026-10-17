"""长期记忆测试：自动提取、向量去重、语义召回、停用治理与 REST 契约。

覆盖「提取成功的结构化输出 / 输出非法时不影响主链路 / 去重跳过 / 停用后不召回 /
未启用时 503」等关键分支。
"""

from __future__ import annotations

import json

import pytest

from app.agent.memory import MemoryService, _parse_candidates
from app.errors import ApiError
from tests.conftest import FailingEmbedder, ScriptedLlm, auth


def memory_service(kb_store, stub_embedder, llm=None, **kwargs) -> MemoryService:
    return MemoryService(kb_store, stub_embedder, llm, **kwargs)


# ============================ 候选解析 ============================


def test_parse_candidates_accepts_fenced_json():
    raw = '```json\n{"memories": [{"text": "用户偏好先出协议", "type": "preference", "importance": "high"}]}\n```'
    candidates = _parse_candidates(raw)
    assert len(candidates) == 1
    assert candidates[0]["type"] == "preference" and candidates[0]["importance"] == "high"


def test_parse_candidates_tolerates_noise_and_invalid_enum():
    raw = '好的，结果如下：{"memories": [{"text": "测试环境只有一套", "type": "不存在的类型", "importance": "very-high"}]}'
    candidates = _parse_candidates(raw)
    assert len(candidates) == 1
    assert candidates[0]["type"] == "other" and candidates[0]["importance"] == "medium"


def test_parse_candidates_returns_empty_for_garbage():
    assert _parse_candidates("完全不是 JSON") == []
    assert _parse_candidates('{"memories": "x"}') == []
    assert _parse_candidates('{"memories": [{"text": "   "}]}') == []


# ============================ 写入 / 去重 / 召回 ============================


async def test_add_memory_then_dedup_identical_text(kb_store, stub_embedder):
    service = memory_service(kb_store, stub_embedder)
    first = await service.add_memory("用户要求所有接口先出协议再开发", memory_type="preference", importance="high")
    assert first["created"] is True and first["status"] == "active"

    second = await service.add_memory("用户要求所有接口先出协议再开发", memory_type="preference", importance="high")
    assert second["created"] is False
    assert second["memoryId"] == first["memoryId"]
    assert kb_store.count("memories") == 1


async def test_add_memory_rejects_blank_text(kb_store, stub_embedder):
    service = memory_service(kb_store, stub_embedder)
    with pytest.raises(ApiError):
        await service.add_memory("   ")


async def test_recall_returns_relevant_memory_only(kb_store, stub_embedder):
    service = memory_service(kb_store, stub_embedder)
    await service.add_memory("测试环境只有一套，联调需排队", memory_type="constraint", importance="high")
    await service.add_memory("发布流程采用灰度发布", memory_type="fact")

    hits = await service.recall("联调环境怎么安排")
    assert hits and "联调" in hits[0]["text"]
    assert all("联调" in hit["text"] or hit["score"] > 0 for hit in hits)
    # 召回会回写 lastUsedAt（验证元数据更新链路）——通过再次读取清单确认持久化结果
    listed = {item["memoryId"]: item for item in await service.list_memories()}
    assert listed[hits[0]["memoryId"]]["lastUsedAt"]


async def test_disabled_memory_is_excluded_from_recall(kb_store, stub_embedder):
    service = memory_service(kb_store, stub_embedder)
    memory = await service.add_memory("测试环境只有一套，联调需排队", memory_type="constraint")
    assert (await service.recall("联调环境怎么安排"))

    updated = await service.set_status(memory["memoryId"], "disabled")
    assert updated["status"] == "disabled"
    assert await service.recall("联调环境怎么安排") == []
    assert (await service.list_memories(status="disabled"))[0]["memoryId"] == memory["memoryId"]

    await service.set_status(memory["memoryId"], "active")
    assert await service.recall("联调环境怎么安排")


async def test_set_status_rejects_invalid_value(kb_store, stub_embedder):
    service = memory_service(kb_store, stub_embedder)
    memory = await service.add_memory("某条记忆")
    with pytest.raises(ApiError):
        await service.set_status(memory["memoryId"], "unknown")


async def test_delete_and_clear_memories(kb_store, stub_embedder):
    service = memory_service(kb_store, stub_embedder)
    first = await service.add_memory("发布必须灰度")
    await service.add_memory("数据库变更需评审")

    removed = await service.delete_memory(first["memoryId"])
    assert removed["deleted"] is True
    with pytest.raises(ApiError):
        await service.delete_memory(first["memoryId"])

    cleared = await service.clear_all()
    assert cleared["deleted"] == 1
    assert await service.stats() == {
        "total": 0,
        "active": 0,
        "disabled": 0,
        "byType": {},
        "maxItems": 2000,
        "autoExtract": False,
        "topK": 4,
    }


async def test_stats_groups_by_type_and_status(kb_store, stub_embedder):
    service = memory_service(kb_store, stub_embedder)
    await service.add_memory("事实一", memory_type="fact")
    second = await service.add_memory("决策一", memory_type="decision")
    await service.set_status(second["memoryId"], "disabled")

    stats = await service.stats()
    assert stats["total"] == 2 and stats["active"] == 1 and stats["disabled"] == 1
    assert stats["byType"] == {"fact": 1, "decision": 1}


async def test_build_context_is_none_when_no_memory(kb_store, stub_embedder):
    service = memory_service(kb_store, stub_embedder)
    context, items = await service.context_and_memories("任意问题")
    assert context is None and items == []


async def test_context_degrades_on_embedder_failure(kb_store):
    service = MemoryService(kb_store, FailingEmbedder())
    context, items = await service.context_and_memories("任意问题")
    assert context is None and items == []


# ============================ 自动提取 ============================


async def test_extract_stores_memories_from_llm_output(kb_store, stub_embedder):
    llm = ScriptedLlm(
        [
            {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "memories": [
                            {"text": "用户偏好先出接口协议再开发", "type": "preference", "importance": "high", "tags": ["流程"]},
                            {"text": "本轮不含联调", "type": "decision", "importance": "medium"},
                        ]
                    },
                    ensure_ascii=False,
                ),
            }
        ]
    )
    service = memory_service(kb_store, stub_embedder, llm, extract_enabled=True)
    assert service.auto_extract_enabled is True

    stored = await service.extract("s1", "我们有个偏好：接口先出协议再开发，另外本轮不含联调。", "好的，已记录。")
    assert len(stored) == 2
    assert {item["type"] for item in stored} == {"preference", "decision"}
    assert stored[0]["source"] == "s1"
    assert kb_store.count("memories") == 2


async def test_extract_skips_when_llm_output_invalid(kb_store, stub_embedder):
    llm = ScriptedLlm([{"role": "assistant", "content": "抱歉，我不知道该记什么。"}])
    service = memory_service(kb_store, stub_embedder, llm, extract_enabled=True)
    assert await service.extract("s1", "随便聊聊", "好的") == []
    assert kb_store.count("memories") == 0


async def test_extract_disabled_without_llm(kb_store, stub_embedder):
    service = memory_service(kb_store, stub_embedder, None, extract_enabled=True)
    assert service.auto_extract_enabled is False
    assert await service.extract("s1", "我们约定灰度发布", "收到") == []


# ============================ REST 契约 ============================


def test_memory_endpoints_disabled_without_embedder(client):
    response = client.get("/api/memories", headers=auth())
    assert response.status_code == 503
    assert response.json()["code"] == "EMBEDDING_UNAVAILABLE"


def test_memory_full_rest_flow(kb_client):
    with kb_client() as c:
        headers = auth()
        created = c.post(
            "/api/memories",
            json={"text": "测试环境只有一套，联调需排队", "type": "constraint", "importance": "high", "tags": ["环境"]},
            headers=headers,
        )
        assert created.status_code == 200
        memory_id = created.json()["data"]["memoryId"]

        # 重复写入应被去重（created=false），总数保持 1
        duplicate = c.post("/api/memories", json={"text": "测试环境只有一套，联调需排队"}, headers=headers)
        assert duplicate.json()["data"]["created"] is False

        listing = c.get("/api/memories", headers=headers).json()["data"]
        assert listing["total"] == 1

        found = c.post("/api/memories/search", json={"query": "联调怎么安排"}, headers=headers).json()["data"]
        assert found["items"] and found["items"][0]["memoryId"] == memory_id

        disabled = c.patch(f"/api/memories/{memory_id}", json={"status": "disabled"}, headers=headers)
        assert disabled.status_code == 200 and disabled.json()["data"]["status"] == "disabled"
        assert c.post("/api/memories/search", json={"query": "联调怎么安排"}, headers=headers).json()["data"]["items"] == []

        stats = c.get("/api/memories/stats", headers=headers).json()["data"]
        assert stats["total"] == 1 and stats["disabled"] == 1 and stats["active"] == 0

        assert c.delete(f"/api/memories/{memory_id}", headers=headers).status_code == 200
        assert c.delete(f"/api/memories/{memory_id}", headers=headers).status_code == 404

        c.post("/api/memories", json={"text": "再来一条"}, headers=headers)
        cleared = c.delete("/api/memories", headers=headers).json()["data"]
        assert cleared["deleted"] == 1


def test_memory_status_validation(kb_client):
    with kb_client() as c:
        headers = auth()
        memory_id = c.post("/api/memories", json={"text": "条目"}, headers=headers).json()["data"]["memoryId"]
        assert c.patch(f"/api/memories/{memory_id}", json={"status": "nope"}, headers=headers).status_code == 400
        assert c.patch("/api/memories/missing", json={"status": "active"}, headers=headers).status_code == 404


# ============================ Agent 编排集成 ============================


async def test_schedule_extract_runs_in_background(kb_store, stub_embedder):
    """``schedule_extract`` 应在后台完成提取入库（不阻塞调用方）。"""
    import asyncio

    llm = ScriptedLlm(
        [{"role": "assistant", "content": '{"memories": [{"text": "用户团队约定联调需排队", "type": "constraint"}]}'}]
    )
    service = MemoryService(kb_store, stub_embedder, llm, extract_enabled=True)
    service.schedule_extract("s1", "我们约定联调需排队", "好的，已记录")
    await asyncio.sleep(0.1)  # 让后台任务完成（仅测试用，避免轮询）

    items = await service.list_memories()
    assert len(items) == 1
    assert items[0]["source"] == "s1" and "联调" in items[0]["text"]


def test_chat_injects_memory_and_knowledge(kb_client):
    """每轮对话应把既有长期记忆与知识库片段注入 system 区块，并回传引用 / 记忆卡片。"""
    llm = ScriptedLlm([{"role": "assistant", "content": "依据规范：关键路径由 AOE 算法计算。"}])
    # 关闭自动提取，避免后台任务与本用例的脚本化 LLM 响应争用
    with kb_client(llm=llm, overrides={"memory_extract": False}) as c:
        headers = auth()
        seeded_doc = c.post(
            "/api/kb/documents",
            json={"title": "排期规范", "content": "关键路径必须由 AOE 算法计算，不允许手工排期。"},
            headers=headers,
        )
        assert seeded_doc.is_success, f"知识库播种失败：{seeded_doc.status_code} {seeded_doc.text}"
        seeded_memory = c.post(
            "/api/memories", json={"text": "关键路径怎么算都按 AOE 结果"}, headers=headers
        )
        assert seeded_memory.is_success, f"记忆播种失败：{seeded_memory.status_code} {seeded_memory.text}"

        data = c.post("/api/agent/chat", json={"message": "关键路径怎么算？"}, headers=headers).json()["data"]
        assert data["references"], "应返回知识库引用片段"
        assert data["memories"] and "关键路径" in data["memories"][0]["text"]

        system_blocks = [message["content"] for message in llm.calls[0] if message["role"] == "system"]
        assert any("长期记忆" in block for block in system_blocks)
        assert any("知识库检索到的相关片段" in block for block in system_blocks)


def test_chat_uses_kb_search_tool(kb_client):
    """agentic RAG：模型可主动调用只读检索工具，结果回填后继续作答。"""
    from tests.conftest import tool_call

    llm = ScriptedLlm(
        [
            {"role": "assistant", "content": None, "tool_calls": [tool_call("kb_search", {"query": "排期规范"}, "c1")]},
            {"role": "assistant", "content": "依据规范：关键路径由 AOE 计算。"},
        ]
    )
    with kb_client(llm=llm, overrides={"memory_extract": False}) as c:
        headers = auth()
        seeded = c.post(
            "/api/kb/documents",
            json={"title": "排期规范", "content": "关键路径必须由 AOE 算法计算。"},
            headers=headers,
        )
        assert seeded.is_success, f"知识库播种失败：{seeded.status_code} {seeded.text}"
        data = c.post("/api/agent/chat", json={"message": "查一下规范"}, headers=headers).json()["data"]
        assert data["toolCalls"][0]["name"] == "kb_search"
        assert data["toolCalls"][0]["ok"] is True
        assert data["toolCalls"][0]["result"]["count"] >= 1
        assert "AOE" in data["content"]


def test_kb_tools_absent_without_embedder(make_client):
    """未启用向量化时，检索类工具不应出现在工具清单里（避免模型调用必然失败的工具）。"""
    with make_client() as c:
        tools = [item["function"]["name"] for item in c.get("/api/agent/tools", headers=auth()).json()["data"]]
        assert "kb_search" not in tools and "memory_search" not in tools

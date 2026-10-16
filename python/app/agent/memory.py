"""长期记忆（Mem0 风格：自动提取 + 语义召回）。

闭环流程：

```
每轮对话 → LLM 抽取候选记忆(JSON) → 逐条向量化 → 与既有记忆做**向量去重**
        → 写入向量库(memories 集合) → 下一轮按当前提问**语义召回** top-k → 注入 system 区块
```

设计要点：
- **自动提取**：只在配置了 LLM 时启用；抽取失败仅告警，**绝不影响主对话链路**；
- **去重**：候选记忆向量与既有记忆的最高相似度 ≥ ``dedup_threshold``（默认 0.92）即视为重复，
  跳过写入（同时避免同一偏好在多轮对话中被反复记录）；
- **可治理**：支持列表 / 停用（保留但不参与召回）/ 启用 / 删除 / 清空，便于人工纠偏；
- **可观测**：提取、去重、召回分别埋点，便于分析记忆质量。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from ..constants import (
    MEMORY_COLLECTION,
    MEMORY_IMPORTANCE,
    MEMORY_STATUS_ACTIVE,
    MEMORY_STATUS_DISABLED,
    MEMORY_TYPES,
)
from ..errors import ApiError, bad_request, not_found
from ..kb.embedder import Embedder
from ..kb.store import ChromaVectorStore
from ..metrics import METRICS
from .llm import LlmClient

logger = logging.getLogger("iteration_planner.memory")

#: 注入上下文的记忆条数上限（防止记忆塞满上下文）
_DEFAULT_TOP_K = 4
#: 召回相似度下限（低于该值视为不相关，不注入）
_MIN_RECALL_SCORE = 0.25
#: 重要度权重（高重要度在同等相关度下优先注入）
_IMPORTANCE_BOOST = {"high": 0.12, "medium": 0.04, "low": 0.0}
#: 记忆文本长度限制
_MAX_TEXT_CHARS = 500

_CODE_FENCE = re.compile(r"```(?:json)?(.*?)```", re.DOTALL)

_EXTRACTION_PROMPT = """你是「迭代计划助手」的长期记忆提取器。请从下面这一轮对话中，抽取**值得长期记住**的用户信息。

只抽取满足以下条件的信息：
1. 用户表达的项目背景、业务事实、团队情况（fact）；
2. 用户的偏好或习惯（preference），例如「接口必须先出协议再开发」；
3. 用户已明确拍板的决策（decision），例如「本轮排期不含联调」；
4. 用户给出的硬性约束（constraint），例如「测试环境只有一套」；
5. 用户交办且需要后续跟进的事项（task）。

不要抽取：寒暄、与本项目无关的闲聊、助手自己说的话、临时的一次性查询、已经从对话中能直接读出的计划内容。

输出**严格 JSON**（不要任何解释文字）：
{"memories": [{"text": "一句话描述（尽量自包含，含必要主语）", "type": "fact|preference|decision|constraint|task|other", "importance": "high|medium|low", "tags": ["最多3个短标签"]}]}

若没有值得长期记住的信息，输出 {"memories": []}。
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryService:
    """长期记忆服务（提取 / 去重 / 召回 / 治理）。"""

    def __init__(
        self,
        store: ChromaVectorStore,
        embedder: Embedder,
        llm: LlmClient | None = None,
        *,
        top_k: int = _DEFAULT_TOP_K,
        dedup_threshold: float = 0.92,
        max_items: int = 2000,
        extract_enabled: bool = True,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._llm = llm
        self._top_k = max(1, top_k)
        self._dedup_threshold = dedup_threshold
        self._max_items = max(1, max_items)
        self._extract_enabled = extract_enabled and llm is not None
        #: 后台提取任务强引用（避免被 GC 提前回收）
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def auto_extract_enabled(self) -> bool:
        return self._extract_enabled

    # ------------------------------- 召回 / 注入 -------------------------------

    async def recall(self, query: str, *, top_k: int | None = None) -> list[dict[str, Any]]:
        """按语义召回长期记忆（仅启用状态，相关度低于下限的丢弃）。"""
        cleaned = (query or "").strip()
        if not cleaned:
            return []
        if await asyncio.to_thread(self._store.count, MEMORY_COLLECTION) == 0:
            return []
        limit = max(1, int(top_k or self._top_k))
        vector = (await self._embedder.embed([cleaned]))[0]
        rows = await asyncio.to_thread(
            self._store.query,
            MEMORY_COLLECTION,
            embedding=vector,
            top_k=max(limit * 3, limit),
            where={"status": MEMORY_STATUS_ACTIVE},
        )
        memories = [self._to_memory(row) for row in rows if float(row.get("score") or 0.0) >= _MIN_RECALL_SCORE]
        # 相关度 + 重要度加权排序：高重要度的长期偏好优先进入上下文
        memories.sort(key=lambda item: float(item["score"]) + _IMPORTANCE_BOOST.get(item["importance"], 0.0), reverse=True)
        selected = memories[:limit]
        if selected:
            await asyncio.to_thread(self._touch, [item["memoryId"] for item in selected])
            _record_recall(len(selected))
        return selected

    def _touch(self, memory_ids: list[str]) -> None:
        """更新最近使用时间（仅元数据更新，不重算向量；同步执行，由 ``to_thread`` 包裹）。"""
        stamp = _now_iso()
        rows = self._store.get_by_ids(MEMORY_COLLECTION, ids=memory_ids, include_documents=False)
        ids: list[str] = []
        metadatas: list[dict[str, Any]] = []
        for row in rows:
            ids.append(str(row["id"]))
            metadatas.append({**(row.get("metadata") or {}), "lastUsedAt": stamp})
        self._store.update_metadata(MEMORY_COLLECTION, ids=ids, metadatas=metadatas)

    async def context_and_memories(
        self, query: str, *, top_k: int | None = None
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """同时返回「可注入的记忆区块」与「召回的记忆列表」（供前端展示模型记得什么）。"""
        try:
            memories = await self.recall(query, top_k=top_k)
        except ApiError as exc:
            logger.warning("长期记忆召回降级（%s）：%s", exc.code, exc.message)
            return None, []
        except Exception as exc:  # noqa: BLE001 - 兜底：增强能力绝不阻断主链路
            logger.warning("长期记忆召回出现未预期异常，已跳过：%s", exc)
            return None, []
        if not memories:
            return None, []
        lines = ["以下是关于当前用户 / 项目的长期记忆（仅作背景参考，可能与当前请求无关）："]
        for item in memories:
            lines.append(f"- [{item['type']}] {item['text']}")
        return "\n".join(lines), memories

    async def build_context(self, query: str, *, top_k: int | None = None) -> str | None:
        """把召回记忆拼成可注入 system 的区块（无命中返回 ``None``）。"""
        return (await self.context_and_memories(query, top_k=top_k))[0]

    # ------------------------------- 提取 -------------------------------

    async def extract(
        self,
        session_id: str,
        user_message: str,
        assistant_message: str,
    ) -> list[dict[str, Any]]:
        """从一轮对话中抽取并写入记忆（返回本轮实际写入的条目）。"""
        if not self._extract_enabled or self._llm is None:
            return []
        candidates = await self._propose(user_message, assistant_message)
        if not candidates:
            return []
        stored: list[dict[str, Any]] = []
        for candidate in candidates:
            try:
                memory = await self.add_memory(
                    candidate["text"],
                    memory_type=candidate["type"],
                    importance=candidate["importance"],
                    tags=candidate["tags"],
                    source=session_id,
                )
            except ApiError as exc:
                logger.warning("记忆写入失败（%s）：%s", exc.code, exc.message)
                continue
            if memory.get("created"):
                stored.append(memory)
                _record_extracted()
        return stored

    def schedule_extract(self, session_id: str, user_message: str, assistant_message: str) -> None:
        """后台触发记忆提取（不阻塞对话响应；异常仅记录日志）。"""
        if not self._extract_enabled:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - 无事件循环时直接跳过
            return
        task = loop.create_task(self.extract(session_id, user_message, assistant_message))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _propose(self, user_message: str, assistant_message: str) -> list[dict[str, Any]]:
        """调用 LLM 抽取候选记忆，失败返回空列表（不影响主链路）。"""
        assert self._llm is not None
        payload = f"【用户输入】\n{user_message}\n\n【助手回复】\n{assistant_message[:2000]}"
        try:
            message = await self._llm.chat(
                [
                    {"role": "system", "content": _EXTRACTION_PROMPT},
                    {"role": "user", "content": payload},
                ],
                temperature=0.0,
            )
        except ApiError as exc:
            logger.warning("记忆提取调用失败（%s）：%s", exc.code, exc.message)
            return []
        return _parse_candidates(_content_to_text(message.get("content")))

    # ------------------------------- 治理 -------------------------------

    async def add_memory(
        self,
        text: str,
        *,
        memory_type: str = "fact",
        importance: str = "medium",
        tags: list[str] | None = None,
        source: str = "",
    ) -> dict[str, Any]:
        """新增一条记忆；与既有记忆高度相似时跳过（返回 ``created=False``）。"""
        cleaned = _normalize_text(text)
        resolved_type = memory_type if memory_type in MEMORY_TYPES else "other"
        resolved_importance = importance if importance in MEMORY_IMPORTANCE else "medium"
        resolved_tags = [str(tag).strip()[:40] for tag in (tags or []) if str(tag).strip()][:5]

        if await asyncio.to_thread(self._store.count, MEMORY_COLLECTION) >= self._max_items:
            raise bad_request(f"长期记忆已达上限 {self._max_items} 条，请先清理后再写入")

        vector = (await self._embedder.embed([cleaned]))[0]
        duplicate = await self._find_duplicate(vector)
        if duplicate is not None:
            _record_deduped()
            existing = self._to_memory(duplicate)
            existing["created"] = False
            return existing

        memory_id = uuid.uuid4().hex
        created_at = _now_iso()
        metadata = {
            "type": resolved_type,
            "importance": resolved_importance,
            "tags": ",".join(resolved_tags),
            "source": str(source or "")[:128],
            "status": MEMORY_STATUS_ACTIVE,
            "createdAt": created_at,
            "lastUsedAt": "",
        }
        await asyncio.to_thread(
            self._store.upsert,
            MEMORY_COLLECTION,
            ids=[memory_id],
            documents=[cleaned],
            embeddings=[vector],
            metadatas=[metadata],
        )
        logger.info("写入长期记忆：%s（%s / %s）", cleaned[:40], resolved_type, resolved_importance)
        return {
            "memoryId": memory_id,
            "text": cleaned,
            "type": resolved_type,
            "importance": resolved_importance,
            "tags": resolved_tags,
            "source": source or "",
            "status": MEMORY_STATUS_ACTIVE,
            "createdAt": created_at,
            "lastUsedAt": "",
            "score": 1.0,
            "created": True,
        }

    async def list_memories(self, *, status: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        """列出记忆（按创建时间倒序）。"""
        where = {"status": status} if status in (MEMORY_STATUS_ACTIVE, MEMORY_STATUS_DISABLED) else None
        rows = await asyncio.to_thread(self._store.get, MEMORY_COLLECTION, where=where)
        memories = [self._to_memory(row) for row in rows]
        memories.sort(key=lambda item: str(item.get("createdAt") or ""), reverse=True)
        return memories[: max(1, limit)]

    async def set_status(self, memory_id: str, status: str) -> dict[str, Any]:
        """启用 / 停用一条记忆（停用后保留数据但不参与召回）。"""
        if status not in (MEMORY_STATUS_ACTIVE, MEMORY_STATUS_DISABLED):
            raise bad_request(f"非法的记忆状态：{status}")
        target = await self._locate(memory_id)
        if target is None:
            raise not_found(f"记忆不存在：{memory_id}")
        metadata = {**(target.get("metadata") or {}), "status": status}
        await asyncio.to_thread(
            self._store.update_metadata,
            MEMORY_COLLECTION,
            ids=[memory_id],
            metadatas=[metadata],
        )
        return self._to_memory({"id": memory_id, "document": target.get("document") or "", "metadata": metadata})

    async def delete_memory(self, memory_id: str) -> dict[str, Any]:
        """删除一条记忆。"""
        target = await self._locate(memory_id)
        if target is None:
            raise not_found(f"记忆不存在：{memory_id}")
        await asyncio.to_thread(self._store.delete_by_ids, MEMORY_COLLECTION, ids=[memory_id])
        return {"memoryId": memory_id, "deleted": True}

    async def clear_all(self) -> dict[str, Any]:
        """清空全部长期记忆。"""
        total = await asyncio.to_thread(self._store.count, MEMORY_COLLECTION)
        if total:
            await asyncio.to_thread(self._store.reset, MEMORY_COLLECTION)
        return {"deleted": total}

    async def stats(self) -> dict[str, Any]:
        """记忆统计（总数 / 启用 / 停用 / 类型分布）。"""
        rows = await asyncio.to_thread(self._store.get, MEMORY_COLLECTION, include_documents=False)
        by_status: dict[str, int] = {MEMORY_STATUS_ACTIVE: 0, MEMORY_STATUS_DISABLED: 0}
        by_type: dict[str, int] = {}
        for row in rows:
            meta = row.get("metadata") or {}
            status = str(meta.get("status") or MEMORY_STATUS_ACTIVE)
            by_status[status] = by_status.get(status, 0) + 1
            memory_type = str(meta.get("type") or "other")
            by_type[memory_type] = by_type.get(memory_type, 0) + 1
        return {
            "total": len(rows),
            "active": by_status.get(MEMORY_STATUS_ACTIVE, 0),
            "disabled": by_status.get(MEMORY_STATUS_DISABLED, 0),
            "byType": by_type,
            "maxItems": self._max_items,
            "autoExtract": self._extract_enabled,
            "topK": self._top_k,
        }

    # ------------------------------- 内部实现 -------------------------------

    async def _locate(self, memory_id: str) -> dict[str, Any] | None:
        """按主键定位记忆。"""
        rows = await asyncio.to_thread(self._store.get_by_ids, MEMORY_COLLECTION, ids=[memory_id])
        return rows[0] if rows else None

    async def _find_duplicate(self, vector: list[float]) -> dict[str, Any] | None:
        """向量去重：与既有记忆的最高相似度达到阈值即判为重复。"""
        rows = await asyncio.to_thread(
            self._store.query,
            MEMORY_COLLECTION,
            embedding=vector,
            top_k=1,
        )
        if not rows:
            return None
        top = rows[0]
        if float(top.get("score") or 0.0) >= self._dedup_threshold:
            return top
        return None

    @staticmethod
    def _to_memory(row: dict[str, Any]) -> dict[str, Any]:
        meta = row.get("metadata") or {}
        return {
            "memoryId": str(row.get("id") or ""),
            "text": row.get("document") or "",
            "type": str(meta.get("type") or "other"),
            "importance": str(meta.get("importance") or "medium"),
            "tags": [tag for tag in str(meta.get("tags") or "").split(",") if tag],
            "source": str(meta.get("source") or ""),
            "status": str(meta.get("status") or MEMORY_STATUS_ACTIVE),
            "createdAt": str(meta.get("createdAt") or ""),
            "lastUsedAt": str(meta.get("lastUsedAt") or ""),
            "score": float(row.get("score") or 0.0),
        }


# ------------------------------- 指标埋点 -------------------------------


def _record_recall(count: int) -> None:
    METRICS.inc_counter("memory_recall_total", {"outcome": "hit"})
    METRICS.observe("memory_recall_size", float(count), help="单次召回的长期记忆条数", buckets=(1, 2, 3, 4, 6, 8, 10, 20))


def _record_extracted() -> None:
    METRICS.inc_counter("memory_extract_total", {"outcome": "stored"})


def _record_deduped() -> None:
    METRICS.inc_counter("memory_extract_total", {"outcome": "deduped"})


# ------------------------------- 文本工具 -------------------------------


def _normalize_text(text: str) -> str:
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        raise bad_request("记忆内容不能为空")
    return cleaned[:_MAX_TEXT_CHARS]


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(item.get("text", "")) for item in content if isinstance(item, dict) and item.get("type") == "text")
    return "" if content is None else str(content)


def _parse_candidates(raw: str) -> list[dict[str, Any]]:
    """解析 LLM 返回的记忆候选 JSON（容忍 ```json 代码围栏与前后噪声）。"""
    text = (raw or "").strip()
    fenced = _CODE_FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    if not text:
        return []
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        parsed = json.loads(text)
    except ValueError:
        logger.warning("记忆提取输出非合法 JSON，已跳过：%s", raw[:200])
        return []
    items = parsed.get("memories") if isinstance(parsed, dict) else parsed
    if not isinstance(items, list):
        return []
    candidates: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        content = str(item.get("text") or "").strip()
        if not content:
            continue
        memory_type = str(item.get("type") or "other")
        importance = str(item.get("importance") or "medium")
        tags = item.get("tags") if isinstance(item.get("tags"), list) else []
        candidates.append(
            {
                "text": content[:_MAX_TEXT_CHARS],
                "type": memory_type if memory_type in MEMORY_TYPES else "other",
                "importance": importance if importance in MEMORY_IMPORTANCE else "medium",
                "tags": [str(tag).strip() for tag in tags if str(tag).strip()][:5],
            }
        )
    return candidates


__all__ = ["MemoryService"]

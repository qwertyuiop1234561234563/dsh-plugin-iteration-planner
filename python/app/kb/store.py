"""向量库抽象与 Chroma 实现。

采用 **Chroma（``chromadb``）持久化客户端**：本地嵌入式、无需额外服务、主流易运维，
单机即可跑通完整 RAG 流程；集合使用余弦距离（``hnsw:space=cosine``），与归一化向量的
相似度语义一致（``similarity = 1 - distance``）。

两个物理隔离的集合：

- ``kb_chunks``：知识库文档分块（每块一条向量，携带文档级元数据）；
- ``memories``：长期记忆条目（每条记忆一条向量，携带类型 / 重要度 / 状态）。

约定：
- ``chunkIndex == 0`` 的分块同时承载「文档级元数据」，据此可低成本枚举文档清单；
- 所有写入使用 ``upsert``（幂等，重复摄取同一 docId 不会产生重复向量）；
- Chroma 客户端为**同步 API**，上层服务通过 ``asyncio.to_thread`` 调用，避免阻塞事件循环。
"""

from __future__ import annotations

import logging
from typing import Any

from ..errors import vector_store_unavailable

logger = logging.getLogger("iteration_planner.vectorstore")


class ChromaVectorStore:
    """Chroma 向量库封装（两个集合：知识库分块 / 长期记忆）。"""

    def __init__(self, path: str, *, client: Any | None = None, chunk_collection: str, memory_collection: str) -> None:
        self._path = path
        try:
            import chromadb
            from chromadb.config import Settings as ChromaSettings
        except ImportError as exc:  # pragma: no cover - 依赖缺失时给出明确提示
            raise RuntimeError("未安装向量库依赖 chromadb，请执行 `pip install chromadb`") from exc

        try:
            if client is not None:
                self._client = client
            else:
                # 关闭匿名遥测：离线 / 受限网络环境下避免外呼与噪声日志
                self._client = chromadb.PersistentClient(
                    path=path, settings=ChromaSettings(anonymized_telemetry=False)
                )
        except Exception as exc:  # noqa: BLE001 - 目录不可写等环境问题统一转业务错误
            raise vector_store_unavailable(f"向量库初始化失败（path={path}）：{exc}") from exc

        self._chunk_name = chunk_collection
        self._memory_name = memory_collection
        self._chunks = self._collection(chunk_collection)
        self._memories = self._collection(memory_collection)

    # ------------------------------- 集合访问 -------------------------------

    def _collection(self, name: str) -> Any:
        try:
            return self._client.get_or_create_collection(name=name, metadata={"hnsw:space": "cosine"})
        except Exception as exc:  # noqa: BLE001
            raise vector_store_unavailable(f"向量库集合初始化失败（{name}）：{exc}") from exc

    @property
    def chunks(self) -> Any:
        return self._chunks

    @property
    def memories(self) -> Any:
        return self._memories

    # ------------------------------- 通用操作 -------------------------------

    def upsert(
        self,
        collection: str,
        *,
        ids: list[str],
        documents: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict[str, Any]],
    ) -> None:
        """幂等写入（新增或覆盖）。"""
        if not ids:
            return
        target = self._pick(collection)
        try:
            target.upsert(ids=ids, documents=documents, embeddings=embeddings, metadatas=metadatas)
        except Exception as exc:  # noqa: BLE001
            raise vector_store_unavailable(f"向量写入失败：{exc}") from exc

    def query(
        self,
        collection: str,
        *,
        embedding: list[float],
        top_k: int,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """向量近邻检索，返回 ``[{id, document, metadata, distance, score}]``（distance 升序）。"""
        target = self._pick(collection)
        count = self.count(collection)
        if count == 0:
            return []
        try:
            result = target.query(
                query_embeddings=[embedding],
                n_results=max(1, min(top_k, count)),
                where=where or None,
                include=["documents", "metadatas", "distances"],
            )
        except Exception as exc:  # noqa: BLE001
            raise vector_store_unavailable(f"向量检索失败：{exc}") from exc
        return _rows_from_query(result)

    def get(
        self,
        collection: str,
        *,
        where: dict[str, Any] | None = None,
        include_documents: bool = True,
    ) -> list[dict[str, Any]]:
        """按元数据条件枚举（无向量计算），返回 ``[{id, document, metadata}]``。"""
        target = self._pick(collection)
        include = ["metadatas"]
        if include_documents:
            include.append("documents")
        try:
            result = target.get(where=where or None, include=include)
        except Exception as exc:  # noqa: BLE001
            raise vector_store_unavailable(f"向量库读取失败：{exc}") from exc
        return _rows_from_get(result, include_documents=include_documents)

    def get_by_ids(
        self,
        collection: str,
        *,
        ids: list[str],
        include_documents: bool = True,
    ) -> list[dict[str, Any]]:
        """按主键批量取回（不触发向量计算）。"""
        if not ids:
            return []
        target = self._pick(collection)
        include = ["metadatas"]
        if include_documents:
            include.append("documents")
        try:
            result = target.get(ids=ids, include=include)
        except Exception as exc:  # noqa: BLE001
            raise vector_store_unavailable(f"向量库读取失败：{exc}") from exc
        return _rows_from_get(result, include_documents=include_documents)

    def update_metadata(self, collection: str, *, ids: list[str], metadatas: list[dict[str, Any]]) -> None:
        """仅更新元数据（不重算向量），用于状态切换 / 命中时间戳维护。"""
        if not ids:
            return
        target = self._pick(collection)
        try:
            target.update(ids=ids, metadatas=metadatas)
        except Exception as exc:  # noqa: BLE001
            raise vector_store_unavailable(f"向量库元数据更新失败：{exc}") from exc

    def delete(self, collection: str, *, where: dict[str, Any]) -> None:
        """按元数据条件删除。"""
        target = self._pick(collection)
        try:
            target.delete(where=where)
        except Exception as exc:  # noqa: BLE001
            raise vector_store_unavailable(f"向量库删除失败：{exc}") from exc

    def delete_by_ids(self, collection: str, *, ids: list[str]) -> None:
        """按主键删除（元数据里未冗余主键时使用）。"""
        if not ids:
            return
        target = self._pick(collection)
        try:
            target.delete(ids=ids)
        except Exception as exc:  # noqa: BLE001
            raise vector_store_unavailable(f"向量库删除失败：{exc}") from exc

    def reset(self, collection: str) -> None:
        """清空集合（删除后重建，保持集合定义一致）。"""
        name = self._name(collection)
        try:
            self._client.delete_collection(name=name)
        except Exception as exc:  # noqa: BLE001
            raise vector_store_unavailable(f"向量库清空失败：{exc}") from exc
        fresh = self._collection(name)
        if collection == "chunks":
            self._chunks = fresh
        else:
            self._memories = fresh

    def count(self, collection: str) -> int:
        return int(self._pick(collection).count())

    # ------------------------------- 内部实现 -------------------------------

    def _name(self, collection: str) -> str:
        """把「集合别名 / 集合全名」统一解析为真实集合名。"""
        if collection in ("chunks", self._chunk_name):
            return self._chunk_name
        if collection in ("memories", self._memory_name):
            return self._memory_name
        raise vector_store_unavailable(f"未知向量集合：{collection}")

    def _pick(self, collection: str) -> Any:
        if collection in ("chunks", self._chunk_name):
            return self._chunks
        if collection in ("memories", self._memory_name):
            return self._memories
        raise vector_store_unavailable(f"未知向量集合：{collection}")


def _rows_from_query(result: dict[str, Any]) -> list[dict[str, Any]]:
    ids = (result.get("ids") or [[]])[0]
    documents = (result.get("documents") or [[]])[0] if result.get("documents") else []
    metadatas = (result.get("metadatas") or [[]])[0] if result.get("metadatas") else []
    distances = (result.get("distances") or [[]])[0] if result.get("distances") else []
    rows: list[dict[str, Any]] = []
    for index, item_id in enumerate(ids):
        distance = float(distances[index]) if index < len(distances) and distances[index] is not None else 1.0
        rows.append(
            {
                "id": str(item_id),
                "document": str(documents[index]) if index < len(documents) else "",
                "metadata": dict(metadatas[index]) if index < len(metadatas) and metadatas[index] else {},
                "distance": distance,
                # 余弦距离 → 相似度（并裁剪到 [0,1]，避免浮点噪声越界）
                "score": max(0.0, min(1.0, 1.0 - distance)),
            }
        )
    return rows


def _rows_from_get(result: dict[str, Any], *, include_documents: bool) -> list[dict[str, Any]]:
    ids = result.get("ids") or []
    documents = result.get("documents") or []
    metadatas = result.get("metadatas") or []
    rows: list[dict[str, Any]] = []
    for index, item_id in enumerate(ids):
        rows.append(
            {
                "id": str(item_id),
                "document": str(documents[index]) if include_documents and index < len(documents) else "",
                "metadata": dict(metadatas[index]) if index < len(metadatas) and metadatas[index] else {},
            }
        )
    return rows


__all__ = ["ChromaVectorStore"]

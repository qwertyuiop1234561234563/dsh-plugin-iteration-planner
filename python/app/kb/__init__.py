"""知识库（RAG）与向量检索能力包。

分层（与项目「严格分层」约束一致）：

- ``embedder``：文本向量化（百炼 OpenAI 兼容 ``/embeddings`` 端点）与结果缓存包装；
- ``store``：向量库抽象与 Chroma 实现（分块正文 / 长期记忆两个集合）；
- ``chunking``：递归字符分块（标题 → 段落 → 句子 → 硬切）；
- ``loader``：文档解析（md/txt/csv/json/html/pdf/docx）；
- ``retrieval``：混合检索（稠密向量 + BM25 稀疏 → RRF 融合）；
- ``pipeline``：摄取流水线（解析 → 分块 → 批量向量化 → 入库）与检索编排。

说明：本层只做「文本 → 向量 → 检索」的检索增强，**不参与任何排期 / 依赖 / 风险算法**；
所有算法口径仍以 TypeScript 引擎为唯一事实来源。
"""

from .chunking import chunk_text
from .embedder import BailianEmbedder, CachedEmbedder, Embedder
from .loader import LoadedDocument, load_document
from .pipeline import KnowledgeBaseService, build_knowledge_base
from .retrieval import bm25_search, rrf_fuse, tokenize
from .store import ChromaVectorStore

__all__ = [
    "Embedder",
    "BailianEmbedder",
    "CachedEmbedder",
    "ChromaVectorStore",
    "chunk_text",
    "load_document",
    "LoadedDocument",
    "tokenize",
    "bm25_search",
    "rrf_fuse",
    "KnowledgeBaseService",
    "build_knowledge_base",
]
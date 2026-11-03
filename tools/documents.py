"""当前会话文档的混合检索，工具选择同时承担问答与摘要的意图分流。"""

import asyncio
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from core.exceptions import SemanticSearchUnavailableError
from storage import documents
from tools.document_retrieval import CANDIDATE_LIMIT, fuse_rankings, passages, rank_bm25, select_distinct


class DocumentSearchArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)]
    limit: int = Field(default=4, ge=1, le=4, strict=True)
    file_id: UUID | None = Field(default=None, description="当前会话文件 ID；省略则检索全部可用文本文档")


DOCUMENT_TOOL = {
    "type": "function",
    "function": {
        "name": "search_documents",
        "description": (
            "查询当前会话文件中的具体条款、事实或数据。内部一次完成 BM25 与向量检索、RRF 融合和去重，"
            "返回相关原文与真实位置，按文件名和位置引用。可结合历史改写查询。"
            "向量模型不可用时明确降级为 BM25；片段不代表全文。"
            "全文、章节或广泛主题总结使用 summarize_document；通用知识问题直接回答。"
        ),
        "parameters": DocumentSearchArguments.model_json_schema(),
    },
}


def retrieve(question: str, chunks: list[dict], limit: int = 4) -> list[dict]:
    """保留独立 BM25 入口，返回实际命中的窗口，不截取原始块开头。"""
    return select_distinct(rank_bm25(question, passages(chunks)), limit)


def _hybrid_retrieve(query: str, chunks: list[dict], limit: int) -> dict:
    # 延迟导入和加载本地向量模型，普通聊天与完整摘要不依赖其启动。
    from tools.semantic_search import rank_semantic

    items = passages(chunks)
    lexical = rank_bm25(query, items)
    methods = ["bm25"]
    warning = None
    if items:
        try:
            semantic = rank_semantic(query, items, CANDIDATE_LIMIT)
        except SemanticSearchUnavailableError:
            semantic = []
            warning = "向量模型不可用，本次仅使用 BM25 关键词检索；未执行成功的语义检索不能作为证据。"
        else:
            methods.append("vector")
    else:
        semantic = []
    return {
        "source_type": "document", "query": query,
        "retrieval": {"methods": methods, "fusion": "rrf", "degraded": bool(warning), "warning": warning},
        "results": select_distinct(fuse_rankings(lexical, semantic), limit),
    }


async def available_documents(conversation_id: str | None, file_ids: list[str]) -> tuple[list[str], list[dict]]:
    """准备阶段只校验范围和读取文件清单，不检索正文。"""
    ids = await asyncio.to_thread(documents.resolve_document_ids, conversation_id, file_ids)
    metadata = await asyncio.to_thread(documents.get_document_metadata, ids)
    return ids, metadata


async def search_documents(query: str, file_ids: list[str], limit: int = 4) -> dict:
    """只读取一次当前会话正文，在工作线程完成两路召回与融合。"""
    if not query.strip() or len(query) > 400:
        raise ValueError("query 必须是 1～400 个字符。")
    if type(limit) is not int or not 1 <= limit <= 4:
        raise ValueError("limit 必须是 1～4 的整数。")
    chunks = await asyncio.to_thread(documents.get_document_chunks, file_ids)
    return await asyncio.to_thread(_hybrid_retrieve, query, chunks, limit)

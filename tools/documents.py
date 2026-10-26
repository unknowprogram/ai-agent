"""上传文档检索工具，范围由服务端限定为当前会话的可用文件。"""

import asyncio
import math
import re
from collections import Counter
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from storage import documents


class DocumentSearchArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)]
    limit: int = Field(default=4, ge=1, le=4, strict=True)
    file_id: UUID | None = Field(default=None, description="可选，只能使用系统提供的当前会话文件 ID；省略则检索全部可用文件")


DOCUMENT_TOOL = {
    "type": "function",
    "function": {
        "name": "search_documents",
        "description": (
            "检索用户上传或当前会话已绑定的文档。用户询问文件内容、制度、条款、"
            "数据或文件摘要时，先调用此工具取得真实片段，再依据片段回答并标注文件名和位置。"
            "可结合历史改写 query；返回片段不代表读完全文，概览查询说明覆盖范围。"
        ),
        "parameters": DocumentSearchArguments.model_json_schema(),
    },
}


def _tokens(text: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9_]+", text.lower())
    for sequence in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens.extend(sequence[index:index + 2] for index in range(len(sequence) - 1))
        if len(sequence) == 1:
            tokens.append(sequence)
    return tokens


def retrieve(question: str, chunks: list[dict], limit: int = 4) -> list[dict]:
    """选择相关片段；概览类问题按文件轮流选取开头，不能视为全文总结。"""
    if not chunks:
        return []
    terms = set(_tokens(question))
    counts = [Counter(_tokens(item["content"])) for item in chunks]
    frequency = Counter(term for count in counts for term in terms if term in count)
    scores = []
    for index, (item, count) in enumerate(zip(chunks, counts)):
        score = sum(
            math.log(1 + len(chunks) / (1 + frequency[term])) * min(count[term], 3)
            for term in terms if term in count
        ) / (1 + len(item["content"]) / 1000)
        scores.append((score, index, item))
    overview = bool(re.search(r"总结|概括|概览|主要内容|summary|summarize", question, re.I))
    if overview or not any(score > 0 for score, _, _ in scores):
        groups = {}
        for item in chunks:
            groups.setdefault(item["file_id"], []).append(item)
        selected = []
        offset = 0
        while len(selected) < limit:
            row = [group[offset] for group in groups.values() if len(group) > offset]
            if not row:
                break
            selected.extend(row[:limit - len(selected)])
            offset += 1
    else:
        selected = [item for _, _, item in sorted(scores, key=lambda row: (-row[0], row[1]))[:limit]]
    # 给系统规则和历史留出空间；只返回实际可发送的片段长度。
    return [{**item, "content": item["content"][:700]} for item in selected]


async def available_documents(conversation_id: str | None, file_ids: list[str]) -> tuple[list[str], list[dict]]:
    """准备阶段只校验范围和读取文件清单，不检索正文。"""
    ids = await asyncio.to_thread(documents.resolve_document_ids, conversation_id, file_ids)
    metadata = await asyncio.to_thread(documents.get_document_metadata, ids)
    return ids, metadata


async def search_documents(query: str, file_ids: list[str], limit: int = 4) -> list[dict]:
    """仅在 Agent 调用工具时加载并检索服务端提供的文件范围。"""
    chunks = await asyncio.to_thread(documents.get_document_chunks, file_ids)
    return await asyncio.to_thread(retrieve, query, chunks, limit)

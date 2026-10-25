"""轻量关键词检索，支持中文相邻双字匹配；无需向量数据库。"""

import asyncio
import math
import re
from collections import Counter

from storage import documents


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


async def retrieve_documents(question: str, conversation_id: str | None, file_ids: list[str]):
    ids = await asyncio.to_thread(documents.resolve_document_ids, conversation_id, file_ids)
    chunks = await asyncio.to_thread(documents.get_document_chunks, ids)
    selected = await asyncio.to_thread(retrieve, question, chunks)
    return ids, selected

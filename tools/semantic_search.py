"""当前会话文档的向量语义检索；向量模型仅在调用工具时加载。"""

import asyncio
import os
from functools import lru_cache
from pathlib import Path
from threading import RLock
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from core.exceptions import SemanticSearchUnavailableError
from storage import documents

_EMBED_LOCK = RLock()


class SemanticSearchArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)]
    limit: int = Field(default=4, strict=True, ge=1, le=4)
    file_id: UUID | None = Field(default=None, description="当前会话文档 ID；省略则检索全部可用文档")


SEMANTIC_TOOL = {
    "type": "function",
    "function": {
        "name": "semantic_search",
        "description": (
            "按语义查找当前会话文档片段，适合同义表达、近义词和关键词检索未命中的查询。"
            "返回真实原文、位置和相似度；分数不是正确率。不用于全文摘要或表格统计。"
            "模型不可用时可改用 search_documents，不能声称语义检索已成功。"
        ),
        "parameters": SemanticSearchArguments.model_json_schema(),
    },
}


@lru_cache(maxsize=1)
def _model():
    # 延迟导入，普通聊天、表格与计算不依赖向量模型启动成功。
    # 模型需要预先下载；在线请求不隐式下载大文件。
    try:
        from sentence_transformers import SentenceTransformer
        local_path = Path(__file__).resolve().parents[1] / ".data" / "models" / "bge-small-zh-v1.5"
        default_model = str(local_path) if local_path.is_dir() else "BAAI/bge-small-zh-v1.5"
        return SentenceTransformer(
            os.getenv("SEMANTIC_MODEL_PATH", default_model),
            device=os.getenv("SEMANTIC_DEVICE", "cpu"), local_files_only=True,
        )
    except Exception as exc:
        raise SemanticSearchUnavailableError() from exc


@lru_cache(maxsize=2)
def _encode(texts: tuple[str, ...]):
    # 缓存只包含最近两批文本；正文变化会自动使缓存键变化。
    with _EMBED_LOCK:
        try:
            return _model().encode(
                list(texts), batch_size=32, normalize_embeddings=True,
                convert_to_numpy=True, show_progress_bar=False,
            )
        except SemanticSearchUnavailableError:
            raise
        except Exception as exc:
            raise SemanticSearchUnavailableError() from exc


def _semantic_search(query: str, file_ids: list[str], limit: int) -> list[dict]:
    if not query.strip() or len(query) > 400:
        raise ValueError("query 必须是 1～400 个字符。")
    if type(limit) is not int or not 1 <= limit <= 4:
        raise ValueError("limit 必须是 1～4 的整数。")
    # 范围由 Agent 校验并传入；保留数据库异常，不能伪装成模型故障。
    chunks = documents.get_document_chunks(file_ids)
    passages = []
    for chunk in chunks:
        text = chunk["content"]
        # 将约 1000 字的原始分块拆成 400 字短片段，相邻重叠 80 字。
        # 返回实际命中的短片段，避免只引用原始分块的开头。
        for start in range(0, len(text), 320):
            content = text[start:start + 400]
            if content.strip():
                passages.append({**chunk, "content": content, "char_start": start})
            if start + 400 >= len(text):
                break
    if not passages:
        return []
    try:
        import numpy as np
    except ImportError as exc:
        raise SemanticSearchUnavailableError() from exc
    vectors = _encode(tuple(item["content"] for item in passages))
    query_vector = _encode(("为这个句子生成表示以用于检索相关文章：" + query.strip(),))[0]
    # 归一化向量的点积等于余弦相似度；分数不是答案正确的概率。
    scores = vectors @ query_vector
    selected, seen = [], set()
    for index in np.argsort(-scores, kind="stable"):
        item = passages[int(index)]
        key = (item["file_id"], item["chunk_index"])
        if key in seen:
            continue
        seen.add(key)
        selected.append({**item, "score": round(float(scores[index]), 6)})
        if len(selected) == limit:
            break
    return selected


async def semantic_search(query: str, file_ids: list[str], limit: int = 4) -> list[dict]:
    # 数据库读取和模型推理在线程中执行，不阻塞 FastAPI 事件循环。
    return await asyncio.to_thread(_semantic_search, query, file_ids, limit)


"""协调记忆、搜索、上下文组装及模型调用。"""

from dataclasses import dataclass
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing

from agent.llm import LLMReply, ask_llm, stream_llm
from agent.prompts import SYSTEM_PROMPT
from agent.rag import retrieve_documents
from service.context import build_context
from service.memory import MemoryContext, load_context, save_turn
from tools.web_search import web_search


@dataclass(frozen=True)
class ChatResult:
    answer: LLMReply
    sources: list[dict[str, str]]
    conversation_id: str
    document_sources: list[dict]


@dataclass(frozen=True)
class PreparedChat:
    memory: MemoryContext
    messages: list[dict[str, str]]
    sources: list[dict[str, str]]
    file_ids: list[str]
    document_sources: list[dict]


async def prepare_chat(
    message: str,
    history: list[dict[str, str]],
    *,
    use_web_search: bool = False,
    conversation_id: str | None = None,
    summary: str = "",
    preferences: dict[str, str] | None = None,
    file_ids: list[str] | None = None,
) -> PreparedChat:
    # 1. 读取当前会话的数据库历史。
    memory = await load_context(conversation_id, history)
    document_ids, document_sources = await retrieve_documents(message, conversation_id, file_ids or [])

    # 2. 按需搜索；失败时由全局异常处理器返回错误。
    sources = []

    if use_web_search:
        sources = await web_search(message[:400])

    # 3. 统一组装上下文。
    messages = build_context(
        system_prompt=SYSTEM_PROMPT,
        question=message,
        history=memory.history,
        summary=summary,
        preferences=preferences,
        references=sources,
        searched=use_web_search,
        documents=document_sources,
    )

    included = [item for item in document_sources if json.dumps(item, ensure_ascii=False) in messages[-1]["content"]]
    return PreparedChat(memory=memory, messages=messages, sources=sources, file_ids=document_ids, document_sources=included)


async def chat(
    message: str,
    history: list[dict[str, str]],
    *,
    use_web_search: bool = False,
    conversation_id: str | None = None,
    summary: str = "",
    preferences: dict[str, str] | None = None,
    file_ids: list[str] | None = None,
) -> ChatResult:
    prepared = await prepare_chat(
        message, history, use_web_search=use_web_search,
        conversation_id=conversation_id, summary=summary, preferences=preferences,
        file_ids=file_ids,
    )
    answer = await ask_llm(prepared.messages)

    # 5. 保存原始问题和回答，不把附加搜索资料写成用户问题。
    saved_id = await save_turn(
        prepared.memory,
        message,
        answer.reply,
        prepared.file_ids,
    )

    return ChatResult(
        answer=answer,
        sources=prepared.sources,
        conversation_id=saved_id,
        document_sources=prepared.document_sources,
    )


async def stream_chat(
    message: str, prepared: PreparedChat,
    is_disconnected: Callable[[], Awaitable[bool]],
) -> AsyncIterator[tuple[str, dict]]:
    yield "sources", {"sources": prepared.sources, "document_sources": prepared.document_sources}
    # aclosing 保证浏览器断开或任务取消时关闭 SDK 生成器。
    async with aclosing(stream_llm(prepared.messages)) as chunks:
        async for chunk in chunks:
            if await is_disconnected():
                return
            if isinstance(chunk, str):
                yield "delta", {"text": chunk}
            else:
                saved_id = await save_turn(prepared.memory, message, chunk.reply, prepared.file_ids)
                yield "done", {
                    "conversation_id": saved_id, "reply": chunk.reply,
                    "model": chunk.model, "usage": chunk.usage,
                    "sources": prepared.sources,
                    "document_sources": prepared.document_sources,
                }

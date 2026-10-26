
"""协调记忆、搜索、上下文组装及模型调用。"""

from dataclasses import dataclass
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing

from agent.llm import LLMReply
from agent.runner import AgentResult, run_agent
from agent.prompts import SYSTEM_PROMPT
from tools.documents import available_documents
from tools.document_summary import SUMMARY_CONTEXT_CHARS
from core.exceptions import LLMResponseError
from service.context import MAX_INPUT_CHARS, STRUCTURED_CONTEXT_CHARS, build_context
from service.memory import MemoryContext, load_context, save_turn


@dataclass(frozen=True)
class ChatResult:
    answer: LLMReply
    sources: list[dict[str, str]]
    conversation_id: str
    document_sources: list[dict]
    searched: bool = False
    document_summaries: list[dict] | None = None


@dataclass(frozen=True)
class PreparedChat:
    memory: MemoryContext
    messages: list[dict[str, str]]
    file_ids: list[str]
    available_documents: list[dict]
    question_files: list[dict]
    question: str
    summary: str
    preferences: dict[str, str] | None
    search_mode: bool | None

    def build_messages(self, tool_messages: list[dict], searched: bool) -> list[dict]:
        has_summary = any(item["role"] == "tool" and json.loads(item["content"]).get("source_type") == "document_summary" for item in tool_messages)
        has_structured = any(item["role"] == "tool" and json.loads(item["content"]).get("source_type") in {"calculation", "spreadsheet"} for item in tool_messages)
        return build_context(
            system_prompt=SYSTEM_PROMPT, question=self.question, history=self.memory.history,
            summary=self.summary, preferences=self.preferences, available_documents=self.available_documents,
            search_mode=self.search_mode, tool_messages=tool_messages, searched=searched,
            question_files=self.question_files,
            max_input_chars=SUMMARY_CONTEXT_CHARS if has_summary else STRUCTURED_CONTEXT_CHARS if has_structured else MAX_INPUT_CHARS,
        )


async def prepare_chat(
    message: str,
    history: list[dict[str, str]],
    *,
    use_web_search: bool | None = None,
    conversation_id: str | None = None,
    summary: str = "",
    preferences: dict[str, str] | None = None,
    file_ids: list[str] | None = None,
) -> PreparedChat:
    # 1. 读取当前会话的数据库历史。
    memory = await load_context(conversation_id, history)
    document_ids, metadata = await available_documents(conversation_id, file_ids or [])
    requested_ids = set(file_ids or [])
    question_files = [item for item in metadata if item["file_id"] in requested_ids]

    # 2. 先提供文件清单，正文检索和联网搜索都由模型工具调用决定。
    messages = build_context(
        system_prompt=SYSTEM_PROMPT,
        question=message,
        history=memory.history,
        summary=summary,
        preferences=preferences,
        search_mode=use_web_search,
        available_documents=metadata,
        question_files=question_files,
    )

    return PreparedChat(
        memory=memory, messages=messages, file_ids=document_ids,
        available_documents=metadata, question=message,
        question_files=question_files,
        summary=summary, preferences=preferences, search_mode=use_web_search,
    )


async def chat(
    message: str,
    history: list[dict[str, str]],
    *,
    use_web_search: bool | None = None,
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
    result = None
    async with aclosing(run_agent(
        prepared.build_messages, search_mode=prepared.search_mode, file_ids=prepared.file_ids,
        file_metadata=prepared.available_documents,
    )) as agent:
        async for name, data in agent:
            if name == "result":
                result = data
    if not isinstance(result, AgentResult):
        raise LLMResponseError()
    answer = result.answer

    # 保存原始问题和最终回答；工具过程不写入会话消息。
    saved_id = await save_turn(
        prepared.memory,
        message,
        answer.reply,
        prepared.file_ids,
    )

    return ChatResult(
        answer=answer,
        sources=result.sources,
        conversation_id=saved_id,
        document_sources=result.document_sources,
        searched=result.searched,
        document_summaries=result.document_summaries,
    )


async def stream_chat(
    message: str, prepared: PreparedChat,
    is_disconnected: Callable[[], Awaitable[bool]],
) -> AsyncIterator[tuple[str, dict]]:
    yield "sources", {"sources": [], "document_sources": [], "searched": False}
    # aclosing 保证浏览器断开或任务取消时关闭 SDK 生成器。
    async with aclosing(run_agent(
        prepared.build_messages, search_mode=prepared.search_mode,
        file_ids=prepared.file_ids,
        file_metadata=prepared.available_documents,
        streaming=True, is_disconnected=is_disconnected,
    )) as events:
        async for name, data in events:
            if await is_disconnected():
                return
            if name != "result":
                yield name, data
            else:
                answer = data.answer
                saved_id = await save_turn(prepared.memory, message, answer.reply, prepared.file_ids)
                yield "done", {
                    "conversation_id": saved_id, "reply": answer.reply,
                    "model": answer.model, "usage": answer.usage,
                    "sources": data.sources, "searched": data.searched,
                    "document_sources": data.document_sources,
                    "document_summaries": data.document_summaries,
                }

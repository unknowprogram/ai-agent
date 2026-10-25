"""异步聊天流程与同步 MySQL 存储之间的会话记忆层。"""

import asyncio
from dataclasses import dataclass

from storage import mysql


@dataclass(frozen=True)
class MemoryContext:
    conversation_id: str | None
    history: list[dict[str, str]]
    last_message_id: int = 0


async def load_context(conversation_id: str | None, history: list[dict[str, str]]) -> MemoryContext:
    if conversation_id is None:
        return MemoryContext(None, history)
    snapshot = await asyncio.to_thread(mysql.load_memory, conversation_id)
    return MemoryContext(conversation_id, snapshot["history"], snapshot["last_message_id"])


def trim_context(history: list[dict[str, str]], message: str) -> list[dict[str, str]]:
    """限制模型输入；数据库仍保存完整原文。"""
    context = [{"role": item["role"], "content": item["content"][:8000]} for item in history[-40:]]
    length = len(message) + sum(len(item["content"]) for item in context)
    while context and length > 60000:
        removed = context[:2]
        del context[:2]
        length -= sum(len(item["content"]) for item in removed)
    return context


async def save_turn(context: MemoryContext, message: str, reply: str, file_ids: list[str] | None = None) -> str:
    return await asyncio.to_thread(
        mysql.save_turn, context.conversation_id, message, reply,
        expected_message_id=context.last_message_id, initial_history=context.history,
        file_ids=file_ids,
    )

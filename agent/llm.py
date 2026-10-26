"""DeepSeek 的异步调用封装。"""

import os
from typing import Any
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI

from core.exceptions import LLMConfigError, LLMResponseError

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str

    def as_dict(self) -> dict:
        return {"id": self.id, "type": "function", "function": {
            "name": self.name, "arguments": self.arguments,
        }}


@dataclass(frozen=True)
class LLMReply:
    reply: str
    model: str
    usage: dict[str, int] | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str | None = None


def _tool_options(tools: list[dict] | None, tool_choice: Any) -> dict:
    return {"tools": tools, "tool_choice": tool_choice} if tools else {}


def _validate_calls(calls: tuple[ToolCall, ...]) -> None:
    if len(calls) > 8 or any(
        not call.id or len(call.id) > 256 or not call.name or len(call.name) > 64
        or not call.arguments or len(call.arguments) > 4096 for call in calls
    ):
        raise LLMResponseError()
    if len({call.id for call in calls}) != len(calls):
        raise LLMResponseError()


def _client_settings() -> tuple[str, str, str]:
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
    model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip()
    if not api_key or not base_url or not model:
        raise LLMConfigError()
    return api_key, base_url, model


def _usage_dict(usage) -> dict[str, int] | None:
    if usage is None:
        return None
    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
    }


async def ask_llm(
    messages: list[dict], *, tools: list[dict] | None = None, tool_choice: Any = "auto",
    max_tokens: int = 2048, json_mode: bool = False,
    timeout: float = 60.0, max_retries: int = 1,
) -> LLMReply:
    """发送已包含系统提示词的消息；密钥只在服务端使用。"""
    api_key, base_url, model = _client_settings()

    async with AsyncOpenAI(
        api_key=api_key, base_url=base_url, timeout=timeout, max_retries=max_retries
    ) as client:
        completion = await client.chat.completions.create(
            model=model, messages=messages, max_tokens=max_tokens,
            **({"response_format": {"type": "json_object"}} if json_mode else {}),
            **_tool_options(tools, tool_choice),
        )

    if not completion.choices:
        raise LLMResponseError()
    choice = completion.choices[0]
    calls = tuple(ToolCall(item.id, item.function.name, item.function.arguments)
                  for item in (choice.message.tool_calls or []))
    _validate_calls(calls)
    reply = (choice.message.content or "").strip()
    if calls and (not tools or choice.finish_reason != "tool_calls"):
        raise LLMResponseError()
    if choice.finish_reason == "tool_calls" and not calls:
        raise LLMResponseError()
    if not reply and not calls:
        raise LLMResponseError()
    usage = completion.usage
    return LLMReply(
        reply=reply,
        model=completion.model or model,
        usage=_usage_dict(usage),
        tool_calls=calls,
        finish_reason=choice.finish_reason,
    )


async def stream_llm(
    messages: list[dict], *, tools: list[dict] | None = None, tool_choice: Any = "auto",
    max_tokens: int = 2048,
) -> AsyncIterator[str | LLMReply]:
    """逐段产出正文，正常结束后产出完整结果；关闭生成器即释放上游连接。"""
    api_key, base_url, model = _client_settings()
    parts = []
    usage = None
    finished = False
    finish_reason = None
    pending_calls = {}
    async with AsyncOpenAI(
        api_key=api_key, base_url=base_url, timeout=60.0, max_retries=1,
    ) as client:
        stream = await client.chat.completions.create(
            model=model, messages=messages, max_tokens=max_tokens,
            stream=True, stream_options={"include_usage": True},
            **_tool_options(tools, tool_choice),
        )
        async with stream:
            async for chunk in stream:
                model = chunk.model or model
                if chunk.usage is not None:
                    usage = _usage_dict(chunk.usage)
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                for delta in choice.delta.tool_calls or []:
                    call = pending_calls.setdefault(delta.index, {"id": "", "name": "", "arguments": ""})
                    if delta.id:
                        call["id"] = delta.id
                    if delta.function:
                        call["name"] += delta.function.name or ""
                        call["arguments"] += delta.function.arguments or ""
                    if len(pending_calls) > 8 or len(call["arguments"]) > 4096:
                        raise LLMResponseError()
                text = choice.delta.content
                if text:
                    parts.append(text)
                    yield text
                if choice.finish_reason in {"stop", "length", "tool_calls"}:
                    finished = True
                    finish_reason = choice.finish_reason

    reply = "".join(parts).strip()
    calls = tuple(ToolCall(**pending_calls[index]) for index in sorted(pending_calls))
    _validate_calls(calls)
    if calls and (not tools or finish_reason != "tool_calls"):
        raise LLMResponseError()
    if finish_reason == "tool_calls" and not calls:
        raise LLMResponseError()
    if not finished or (not reply and not calls):
        raise LLMResponseError()
    yield LLMReply(reply=reply, model=model, usage=usage, tool_calls=calls, finish_reason=finish_reason)


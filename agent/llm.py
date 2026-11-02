# ===== 迁移前实现（逐行注释保留，用于对照） =====
# """DeepSeek 的异步调用封装。"""
#
# import os
# from typing import Any
# from collections.abc import AsyncIterator
# from dataclasses import dataclass
# from pathlib import Path
#
# from dotenv import load_dotenv
# from openai import AsyncOpenAI
#
# from core.exceptions import LLMConfigError, LLMResponseError
#
# load_dotenv(Path(__file__).resolve().parents[1] / ".env")
#
#
# @dataclass(frozen=True)
# class ToolCall:
#     id: str
#     name: str
#     arguments: str
#
#     def as_dict(self) -> dict:
#         return {"id": self.id, "type": "function", "function": {
#             "name": self.name, "arguments": self.arguments,
#         }}
#
#
# @dataclass(frozen=True)
# class LLMReply:
#     reply: str
#     model: str
#     usage: dict[str, int] | None = None
#     tool_calls: tuple[ToolCall, ...] = ()
#     finish_reason: str | None = None
#
#
# def _tool_options(tools: list[dict] | None, tool_choice: Any) -> dict:
#     return {"tools": tools, "tool_choice": tool_choice} if tools else {}
#
#
# def _validate_calls(calls: tuple[ToolCall, ...]) -> None:
#     if len(calls) > 8 or any(
#         not call.id or len(call.id) > 256 or not call.name or len(call.name) > 64
#         or not call.arguments or len(call.arguments) > 4096 for call in calls
#     ):
#         raise LLMResponseError()
#     if len({call.id for call in calls}) != len(calls):
#         raise LLMResponseError()
#
#
# def _client_settings() -> tuple[str, str, str]:
#     api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
#     base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
#     model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip()
#     if not api_key or not base_url or not model:
#         raise LLMConfigError()
#     return api_key, base_url, model
#
#
# def _usage_dict(usage) -> dict[str, int] | None:
#     if usage is None:
#         return None
#     return {
#         "prompt_tokens": usage.prompt_tokens,
#         "completion_tokens": usage.completion_tokens,
#         "total_tokens": usage.total_tokens,
#     }
#
#
# async def ask_llm(
#     messages: list[dict], *, tools: list[dict] | None = None, tool_choice: Any = "auto",
#     max_tokens: int = 2048, json_mode: bool = False,
#     timeout: float = 60.0, max_retries: int = 1,
# ) -> LLMReply:
#     """发送已包含系统提示词的消息；密钥只在服务端使用。"""
#     api_key, base_url, model = _client_settings()
#
#     async with AsyncOpenAI(
#         api_key=api_key, base_url=base_url, timeout=timeout, max_retries=max_retries
#     ) as client:
#         completion = await client.chat.completions.create(
#             model=model, messages=messages, max_tokens=max_tokens,
#             **({"response_format": {"type": "json_object"}} if json_mode else {}),
#             **_tool_options(tools, tool_choice),
#         )
#
#     if not completion.choices:
#         raise LLMResponseError()
#     choice = completion.choices[0]
#     calls = tuple(ToolCall(item.id, item.function.name, item.function.arguments)
#                   for item in (choice.message.tool_calls or []))
#     _validate_calls(calls)
#     reply = (choice.message.content or "").strip()
#     if calls and (not tools or choice.finish_reason != "tool_calls"):
#         raise LLMResponseError()
#     if choice.finish_reason == "tool_calls" and not calls:
#         raise LLMResponseError()
#     if not reply and not calls:
#         raise LLMResponseError()
#     usage = completion.usage
#     return LLMReply(
#         reply=reply,
#         model=completion.model or model,
#         usage=_usage_dict(usage),
#         tool_calls=calls,
#         finish_reason=choice.finish_reason,
#     )
#
#
# async def stream_llm(
#     messages: list[dict], *, tools: list[dict] | None = None, tool_choice: Any = "auto",
#     max_tokens: int = 2048,
# ) -> AsyncIterator[str | LLMReply]:
#     """逐段产出正文，正常结束后产出完整结果；关闭生成器即释放上游连接。"""
#     api_key, base_url, model = _client_settings()
#     parts = []
#     usage = None
#     finished = False
#     finish_reason = None
#     pending_calls = {}
#     async with AsyncOpenAI(
#         api_key=api_key, base_url=base_url, timeout=60.0, max_retries=1,
#     ) as client:
#         stream = await client.chat.completions.create(
#             model=model, messages=messages, max_tokens=max_tokens,
#             stream=True, stream_options={"include_usage": True},
#             **_tool_options(tools, tool_choice),
#         )
#         async with stream:
#             async for chunk in stream:
#                 model = chunk.model or model
#                 if chunk.usage is not None:
#                     usage = _usage_dict(chunk.usage)
#                 if not chunk.choices:
#                     continue
#                 choice = chunk.choices[0]
#                 for delta in choice.delta.tool_calls or []:
#                     call = pending_calls.setdefault(delta.index, {"id": "", "name": "", "arguments": ""})
#                     if delta.id:
#                         call["id"] = delta.id
#                     if delta.function:
#                         call["name"] += delta.function.name or ""
#                         call["arguments"] += delta.function.arguments or ""
#                     if len(pending_calls) > 8 or len(call["arguments"]) > 4096:
#                         raise LLMResponseError()
#                 text = choice.delta.content
#                 if text:
#                     parts.append(text)
#                     yield text
#                 if choice.finish_reason in {"stop", "length", "tool_calls"}:
#                     finished = True
#                     finish_reason = choice.finish_reason
#
#     reply = "".join(parts).strip()
#     calls = tuple(ToolCall(**pending_calls[index]) for index in sorted(pending_calls))
#     _validate_calls(calls)
#     if calls and (not tools or finish_reason != "tool_calls"):
#         raise LLMResponseError()
#     if finish_reason == "tool_calls" and not calls:
#         raise LLMResponseError()
#     if not finished or (not reply and not calls):
#         raise LLMResponseError()
#     yield LLMReply(reply=reply, model=model, usage=usage, tool_calls=calls, finish_reason=finish_reason)
#
# ===== 迁移前实现结束 =====

# ===== 当前实现（LangChain / LangGraph） =====
"""通过 LangChain 调用 DeepSeek，保留聊天与摘要使用的原有接口。"""

import json
import os
from collections.abc import AsyncIterator
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, AIMessageChunk, convert_to_messages
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from openai import AsyncOpenAI

from core.exceptions import LLMConfigError, LLMResponseError

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


@dataclass(frozen=True)
class ToolCall:
    """沿用原工具调用结构，arguments 保留原始 JSON 字符串供业务层校验。"""

    id: str
    name: str
    arguments: str

    def as_dict(self) -> dict:
        return {"id": self.id, "type": "function", "function": {
            "name": self.name, "arguments": self.arguments,
        }}


@dataclass(frozen=True)
class LLMReply:
    """LangChain 返回值的兼容结构，聊天和摘要模块无需改变读取方式。"""

    reply: str
    model: str
    usage: dict[str, int] | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str | None = None


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


class _TrackedCompletions:
    """记录实际 SDK 响应流，停止接收时显式关闭，避免等待嵌套生成器回收。"""

    def __init__(self, completions):
        self.completions = completions
        self.streams = []

    def __getattr__(self, name):
        # 普通调用的 with_raw_response 等接口仍直接交给 SDK。
        return getattr(self.completions, name)

    async def create(self, *args, **kwargs):
        response = await self.completions.create(*args, **kwargs)
        if kwargs.get("stream"):
            self.streams.append(response)
        return response

    async def close(self):
        for stream in self.streams:
            await stream.close()


@asynccontextmanager
async def _model(*, max_tokens: int, timeout: float, max_retries: int):
    api_key, base_url, model_name = _client_settings()
    # SDK 只负责连接生命周期；消息转换、工具绑定和请求都经过 LangChain。
    # 显式拥有本次异步客户端，避免取消时关闭 LangChain 缓存的共享连接。
    async with AsyncOpenAI(
        api_key=api_key, base_url=base_url, timeout=timeout, max_retries=max_retries,
    ) as client:
        completions = _TrackedCompletions(client.chat.completions)
        model = ChatOpenAI(
            model=model_name, api_key=api_key, base_url=base_url,
            timeout=timeout, max_retries=max_retries, max_tokens=max_tokens,
            # async_client=client.chat.completions, root_async_client=client,
            async_client=completions, root_async_client=client,
            use_responses_api=False, stream_usage=True,
        )
        try:
            yield model
        finally:
            # 先关闭正在消费的响应流，再退出 SDK 上下文释放本次连接。
            await completions.close()


def _messages(messages: list[dict]) -> list:
    """把项目的 role/content 字典转换成 LangChain 消息对象。"""
    converted = []
    for message in messages:
        if message["role"] == "assistant":
            # 原始参数即使是无效 JSON 也保留，让图中的校验节点返回配对的工具错误。
            converted.append(AIMessage(
                content=message.get("content") or "",
                additional_kwargs={"tool_calls": message["tool_calls"]}
                if message.get("tool_calls") else {},
            ))
        else:
            converted.extend(convert_to_messages([message]))
    return converted


def _bind(model: ChatOpenAI, tools: list[dict | BaseTool] | None, tool_choice: Any):
    # 工具预算耗尽时 tools 为空，直接调用未绑定工具的模型生成最终回答。
    return model.bind_tools(tools, tool_choice=tool_choice) if tools else model


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(
        part if isinstance(part, str) else part.get("text", "")
        for part in content if isinstance(part, str) or part.get("type") == "text"
    )


def _usage_dict(message: AIMessage | AIMessageChunk) -> dict[str, int] | None:
    # LangChain 使用 input/output_tokens；转换回当前 API 的 prompt/completion_tokens。
    usage = message.usage_metadata
    if usage is not None:
        return {
            "prompt_tokens": usage["input_tokens"],
            "completion_tokens": usage["output_tokens"],
            "total_tokens": usage["total_tokens"],
        }
    usage = message.response_metadata.get("token_usage")
    if usage is None:
        return None
    return {key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")}


def _reply(message: AIMessage | AIMessageChunk, *, tools_enabled: bool) -> LLMReply:
    """保留工具原始参数、结束原因和用量，并拒绝不完整的工具调用。"""
    # 优先保留提供商返回的参数字符串，避免重新序列化丢失错误参数的信息。
    raw_calls = message.additional_kwargs.get("tool_calls")
    if raw_calls is not None:
        calls = tuple(ToolCall(
            item.get("id", ""), item.get("function", {}).get("name", ""),
            item.get("function", {}).get("arguments", ""),
        ) for item in raw_calls)
    else:
        calls = tuple(ToolCall(
            item.get("id") or "", item["name"], json.dumps(item["args"], ensure_ascii=False),
        ) for item in message.tool_calls)
        if message.invalid_tool_calls:
            raise LLMResponseError()
    _validate_calls(calls)
    finish_reason = message.response_metadata.get("finish_reason")
    if calls and (not tools_enabled or finish_reason != "tool_calls"):
        raise LLMResponseError()
    if finish_reason == "tool_calls" and not calls:
        raise LLMResponseError()
    reply = _text(message.content).strip()
    if not reply and not calls:
        raise LLMResponseError()
    return LLMReply(
        reply=reply,
        model=message.response_metadata.get("model_name") or _client_settings()[2],
        usage=_usage_dict(message), tool_calls=calls, finish_reason=finish_reason,
    )


async def ask_llm(
    messages: list[dict], *, tools: list[dict | BaseTool] | None = None, tool_choice: Any = "auto",
    max_tokens: int = 2048, json_mode: bool = False,
    timeout: float = 60.0, max_retries: int = 1,
) -> LLMReply:
    """LangChain 普通调用；摘要的 JSON 模式、超时和重试参数保持兼容。"""
    async with _model(max_tokens=max_tokens, timeout=timeout, max_retries=max_retries) as model:
        runnable = _bind(model, tools, tool_choice)
        if json_mode:
            # 摘要内部仍可请求 JSON 对象；解析和覆盖统计继续由摘要模块处理。
            runnable = runnable.bind(response_format={"type": "json_object"})
        try:
            message = await runnable.ainvoke(_messages(messages))
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            # LangChain 对空 choices、非 JSON 或异常消息结构的错误也统一映射为 502。
            raise LLMResponseError() from exc
    return _reply(message, tools_enabled=bool(tools))


async def stream_llm(
    messages: list[dict], *, tools: list[dict | BaseTool] | None = None, tool_choice: Any = "auto",
    max_tokens: int = 2048,
) -> AsyncIterator[str | LLMReply]:
    """LangChain 流式调用；先产出正文，正常结束后产出完整结果。"""
    combined = None
    async with _model(max_tokens=max_tokens, timeout=60.0, max_retries=1) as model:
        runnable = _bind(model, tools, tool_choice)
        try:
            async with aclosing(runnable.astream(_messages(messages))) as chunks:
                async for chunk in chunks:
                    # LangChain 负责合并正文、分段工具参数和流末尾的用量消息。
                    combined = chunk if combined is None else combined + chunk
                    raw_calls = combined.additional_kwargs.get("tool_calls", [])
                    if len(raw_calls) > 8 or any(
                        len(item.get("function", {}).get("arguments", "")) > 4096
                        for item in raw_calls
                    ):
                        raise LLMResponseError()
                    text = _text(chunk.content)
                    if text:
                        yield text
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise LLMResponseError() from exc
    if combined is None or combined.response_metadata.get("finish_reason") not in {
        "stop", "length", "tool_calls",
    }:
        # 没有正常结束标记的半段输出不能作为完整问答保存。
        raise LLMResponseError()
    yield _reply(combined, tools_enabled=bool(tools))

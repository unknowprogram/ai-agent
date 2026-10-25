"""DeepSeek 的异步调用封装。"""

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from openai import AsyncOpenAI

from core.exceptions import LLMConfigError, LLMResponseError

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


@dataclass(frozen=True)
class LLMReply:
    reply: str
    model: str
    usage: dict[str, int] | None = None


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


async def ask_llm(messages: list[dict[str, str]]) -> LLMReply:
    """发送已包含系统提示词的消息；密钥只在服务端使用。"""
    api_key, base_url, model = _client_settings()

    async with AsyncOpenAI(
        api_key=api_key, base_url=base_url, timeout=60.0, max_retries=1
    ) as client:
        completion = await client.chat.completions.create(
            model=model, messages=messages, max_tokens=2048,
        )

    if not completion.choices or not completion.choices[0].message.content:
        raise LLMResponseError()
    reply = completion.choices[0].message.content.strip()
    if not reply:
        raise LLMResponseError()
    usage = completion.usage
    return LLMReply(
        reply=reply,
        model=completion.model or model,
        usage=_usage_dict(usage),
    )


async def stream_llm(messages: list[dict[str, str]]) -> AsyncIterator[str | LLMReply]:
    """逐段产出正文，正常结束后产出完整结果；关闭生成器即释放上游连接。"""
    api_key, base_url, model = _client_settings()
    parts = []
    usage = None
    finished = False
    async with AsyncOpenAI(
        api_key=api_key, base_url=base_url, timeout=60.0, max_retries=1,
    ) as client:
        stream = await client.chat.completions.create(
            model=model, messages=messages, max_tokens=2048,
            stream=True, stream_options={"include_usage": True},
        )
        async with stream:
            async for chunk in stream:
                model = chunk.model or model
                if chunk.usage is not None:
                    usage = _usage_dict(chunk.usage)
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                text = choice.delta.content
                if text:
                    parts.append(text)
                    yield text
                if choice.finish_reason in {"stop", "length"}:
                    finished = True

    reply = "".join(parts).strip()
    if not finished or not reply:
        raise LLMResponseError()
    yield LLMReply(reply=reply, model=model, usage=usage)


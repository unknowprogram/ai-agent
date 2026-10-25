"""聊天 API，异常由全局处理器统一处理。"""

import asyncio
import json
from contextlib import aclosing

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from core.exception_handlers import ERROR_RESPONSES, resolve_exception
from schemas.chat import ChatRequest, ChatResponse
from service.chat import chat, prepare_chat, stream_chat

router = APIRouter(prefix="/api", tags=["聊天"])


def _chat_arguments(request: ChatRequest) -> dict:
    return {
        "message": request.message,
        "history": [item.model_dump() for item in request.history],
        "conversation_id": str(request.conversation_id) if request.conversation_id else None,
        "use_web_search": request.web_search,
        "file_ids": [str(file_id) for file_id in request.file_ids],
        "summary": "用户正在开发企业知识库 Agent，目前已完成聊天和联网搜索。",
        "preferences": {"语言": "中文", "回答风格": "简洁，优先给出可执行步骤"},
    }


@router.post("/chat", response_model=ChatResponse, responses=ERROR_RESPONSES)
async def create_chat(request: ChatRequest) -> ChatResponse:
    result = await chat(**_chat_arguments(request))
    return ChatResponse(
        reply=result.answer.reply, model=result.answer.model,
        usage=result.answer.usage, sources=result.sources,
        conversation_id=result.conversation_id,
        document_sources=result.document_sources,
    )


def _event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post(
    "/chat/stream", response_class=StreamingResponse,
    responses={
        **ERROR_RESPONSES,
        200: {"description": "SSE 事件流：sources、delta、done 或 error", "content": {"text/event-stream": {"schema": {"type": "string"}}}},
    },
)
async def create_chat_stream(request: ChatRequest, http_request: Request):
    # 开始输出前的记忆、搜索和上下文错误仍使用标准 HTTP 错误响应。
    prepared = await prepare_chat(**_chat_arguments(request))

    async def events():
        try:
            async with aclosing(stream_chat(request.message, prepared, http_request.is_disconnected)) as stream:
                async for name, data in stream:
                    yield _event(name, data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 响应头已发送时通过事件传递错误，复用统一状态码和提示文案。
            spec = resolve_exception(http_request, exc)
            yield _event("error", {"status": spec.status_code, "detail": spec.detail})

    return StreamingResponse(
        events(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

"""会话创建、查询与删除接口，供前端和 Swagger 使用。"""

from uuid import UUID

from fastapi import APIRouter, Query, Response, status

from core.exception_handlers import ERROR_RESPONSES
from schemas.conversation import (
    ConversationCreate, ConversationCreated, ConversationDetail, ConversationSummary,
)
from storage import mysql

router = APIRouter(prefix="/api/conversations", tags=["会话记忆"], responses=ERROR_RESPONSES)


@router.post("", response_model=ConversationCreated, status_code=status.HTTP_201_CREATED)
def create_conversation(request: ConversationCreate):
    return ConversationCreated(conversation_id=mysql.create_conversation(request.title))


@router.get("", response_model=list[ConversationSummary])
def list_conversations(limit: int = Query(default=20, ge=1, le=100)):
    return mysql.list_conversations(limit)


@router.get("/{conversation_id}", response_model=ConversationDetail)
def get_conversation(conversation_id: UUID, limit: int = Query(default=80, ge=1, le=100)):
    return mysql.get_conversation(str(conversation_id), limit)


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(conversation_id: UUID):
    mysql.delete_conversation(str(conversation_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)

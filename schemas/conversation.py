"""会话管理接口的数据模型。"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, Field
from schemas.files import DocumentInfo
from schemas.images import GeneratedImage


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] = "新对话"


class ConversationCreated(BaseModel):
    conversation_id: str


class ConversationSummary(BaseModel):
    id: str
    title: str
    created_at: datetime
    updated_at: datetime


class StoredMessage(BaseModel):
    id: int
    role: Literal["user", "assistant", "system", "tool"]
    content: str
    created_at: datetime
    generated_images: list[GeneratedImage] = Field(default_factory=list)


class ConversationDetail(ConversationSummary):
    messages: list[StoredMessage]
    files: list[DocumentInfo] = Field(default_factory=list)

"""聊天接口的数据模型和输入校验规则。"""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from schemas.search import SearchResult
from schemas.files import DocumentSource, DocumentSummary

MessageText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]


class HistoryMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: MessageText


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: MessageText
    conversation_id: UUID | None = Field(default=None, description="首次留空自动创建；后续携带此 ID 自动读取 MySQL 历史")
    history: list[HistoryMessage] = Field(default_factory=list, max_length=40)
    web_search: bool | None = Field(default=None, strict=True, description="省略或 null：Agent 自主决定搜索；true：要求先搜索；false：禁止搜索")
    file_ids: list[UUID] = Field(default_factory=list, max_length=5, description="本条问题随附文件的上传 ID；首次询问须携带，成功绑定后追问可省略")

    @model_validator(mode="after")
    def validate_context(self):
        if self.conversation_id is not None and self.history:
            raise ValueError("已指定 conversation_id 时请省略 history，由服务端读取会话记忆。")
        if len(self.message) + sum(len(item.content) for item in self.history) > 60000:
            raise ValueError("对话内容过长，请开启新对话。")
        return self


class TokenUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatResponse(BaseModel):
    conversation_id: str
    reply: str
    model: str
    usage: TokenUsage | None = None
    sources: list[SearchResult] = Field(default_factory=list, description="本次联网搜索得到的参考来源")
    searched: bool = Field(default=False, description="本轮是否实际成功执行过联网搜索，空结果也为 true")
    document_sources: list[DocumentSource] = Field(default_factory=list, description="Agent 实际调用文档检索工具并加入模型上下文的文件片段")
    document_summaries: list[DocumentSummary] = Field(default_factory=list, description="分段扫描全部已提取文字后汇总的摘要及实际覆盖统计")

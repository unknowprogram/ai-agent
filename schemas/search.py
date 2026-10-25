"""联网搜索请求及结果结构。"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)]
    max_results: int = Field(default=5, ge=1, le=10, strict=True)


class SearchResult(BaseModel):
    title: str
    url: str
    content: str


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResult]

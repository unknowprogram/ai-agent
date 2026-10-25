"""统一的错误响应结构。"""

from pydantic import BaseModel


class ErrorResponse(BaseModel):
    detail: str

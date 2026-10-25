from datetime import datetime
from pydantic import BaseModel,Field

class DocumentInfo(BaseModel):
    file_id: str
    filename: str
    file_type: str
    char_count: int
    chunk_count: int
    preview: str
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime


class DocumentSource(BaseModel):
    file_id: str
    filename: str
    chunk_index: int
    location: str
    content: str

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


class SummaryCoverage(BaseModel):
    total_chunks: int
    processed_chunks: int
    total_batches: int
    processed_batches: int
    all_extracted_text_scanned: bool
    total_locations: int
    start_location: str
    end_location: str
    relevant_chunks: int
    section_count: int
    warnings: list[str] = Field(default_factory=list)
    warning_count: int = 0


class DocumentSummary(BaseModel):
    file_id: str
    filename: str
    query: str
    summary: str
    coverage: SummaryCoverage
    sections: list[str] = Field(default_factory=list)

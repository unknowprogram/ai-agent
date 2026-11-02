"""读取上传图片，并调用独立的 SenseNova 视觉模型。"""

import asyncio
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from agent.vision import analyze_image_bytes
from service.images import IMAGE_TYPES
from storage import documents
from storage.file_data import get_file_data


class ImageArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_id: UUID = Field(description="当前会话文件清单中的图片 ID")
    query: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)
    ] = Field(description="图片分析问题，例如识别截图文字、描述图片、读取图表或表格")


IMAGE_TOOL = {
    "type": "function",
    "function": {
        "name": "analyze_image",
        "description": (
            "分析当前会话已上传的 JPG、JPEG、PNG、WebP 静态图片。"
            "用于图片描述、截图问答、文字识别和图表理解。"
            "必须使用系统文件清单中的图片 ID，不能根据文件名猜测内容。"
            "此工具不读取 PDF 内嵌图片，不处理视频或动画。"
        ),
        "parameters": ImageArguments.model_json_schema(),
    },
}


@dataclass(frozen=True)
class ImageAnalysisResult:
    data: dict
    usage: dict[str, int] | None


async def analyze_image_file(file_id: str, query: str) -> ImageAnalysisResult:
    file_id = str(UUID(file_id))
    document = await asyncio.to_thread(documents.get_document, file_id)
    if document["file_type"] not in IMAGE_TYPES:
        raise ValueError("analyze_image 只能分析当前会话中的图片文件。")

    raw = await asyncio.to_thread(get_file_data, file_id)
    result = await analyze_image_bytes(raw, query)

    # 复用现有 document_sources 结构；这是模型识别结果，不是原文文本块。
    # Base64 和原始图片字节只参与视觉请求，不放进工具结果。
    return ImageAnalysisResult(
        data={
            "source_type": "image",
            "file_id": file_id,
            "filename": document["filename"],
            "query": query,
            "model": result.answer.model,
            "image": result.image,
            "truncated": False,
            "results": [{
                "file_id": file_id,
                "filename": document["filename"],
                "chunk_index": 1,
                "location": "图片视觉识别（模型输出）",
                "content": result.answer.reply,
                "score": None,
                "char_start": None,
            }],
        },
        usage=result.answer.usage,
    )

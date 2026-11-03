"""文生图工具：调用 U1.5 Lite 并保存原件，仅把小型展示元数据返回给 Agent。"""

import asyncio
from datetime import datetime, timezone
from typing import Annotated
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from agent.image_generation import generate_image_bytes
from core.exceptions import (
    FileParseError, FileTooLargeError, ImageGenerationResponseError, UnsupportedFileError,
)
from schemas.images import GeneratedImage, validate_image_size
from service.images import inspect_image
from storage.generated_images import save_image


class GenerateImageArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
    ] = Field(description="用户要求生成的图片描述，包含主体、场景、风格、构图及需要显示的准确文字")
    size: str = Field(
        default="1024x1024",
        description="auto、2K、4K 或宽x高；宽高为 512–4096 内 32 的倍数，长宽比最多 3:1",
    )

    @field_validator("size")
    @classmethod
    def validate_size(cls, value: str) -> str:
        return validate_image_size(value)


GENERATE_IMAGE_TOOL = {
    "type": "function",
    "function": {
        "name": "generate_image",
        "description": (
            "使用 SenseNova U1.5 Lite 根据文字生成一张图片。只在用户明确要求画图、生图、"
            "制作图片或视觉设计时调用；询问生图功能的开发方法时不调用。每轮最多生成一张。"
            "仅支持文生图，不接受参考图片，不可用于图片识别或声称已经修改上传的图片。"
            "返回的本地图片由界面展示，可预览和下载；不能编造成功结果或可见图片细节。"
        ),
        "parameters": GenerateImageArguments.model_json_schema(),
    },
}


async def generate_image(prompt: str, size: str = "1024x1024") -> dict:
    arguments = GenerateImageArguments(prompt=prompt, size=size)
    result = await generate_image_bytes(arguments.prompt, arguments.size)
    try:
        info = await asyncio.to_thread(inspect_image, result.raw)
    except (FileParseError, FileTooLargeError, UnsupportedFileError) as exc:
        raise ImageGenerationResponseError() from exc
    try:
        actual_size = validate_image_size(f"{info.width}x{info.height}")
    except ValueError as exc:
        raise ImageGenerationResponseError() from exc
    if arguments.size not in {"auto", "2K", "4K"} and arguments.size != actual_size:
        raise ImageGenerationResponseError()
    image_id = uuid4()
    image = GeneratedImage(
        image_id=image_id, url=f"/api/images/{image_id}",
        prompt=result.prompt, model=result.model, file_type=info.file_type,
        width=info.width, height=info.height, watermark=result.watermark,
        elapsed_seconds=result.elapsed_seconds, usage=result.usage,
        created_at=datetime.now(timezone.utc),
    )
    await asyncio.to_thread(save_image, result.raw, image)
    return {"source_type": "generated_image", "images": [image.model_dump(mode="json")]}

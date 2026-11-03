"""生成图片的展示信息；图片二进制和服务端密钥不进入聊天响应。"""

import re
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints, model_validator


def validate_image_size(value: str) -> str:
    """按 SenseNova 文档校验尺寸，支持 auto、2K、4K 和具体宽高。"""
    value = value.strip().replace(" ", "").replace("×", "x").lower()
    if value in {"auto", "2k", "4k"}:
        return value if value == "auto" else value.upper()
    match = re.fullmatch(r"(\d{3,4})x(\d{3,4})", value)
    if match:
        width, height = map(int, match.groups())
        if (
            512 <= width <= 4096 and 512 <= height <= 4096
            and width % 32 == 0 and height % 32 == 0
            and max(width, height) <= 3 * min(width, height)
        ):
            return f"{width}x{height}"
    raise ValueError(
        "图片尺寸须为 auto、2K、4K，或 512–4096 范围内、32 的倍数的宽x高；长宽比最多 3:1。"
    )


class GeneratedImage(BaseModel):
    image_id: UUID
    url: str
    prompt: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    model: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    file_type: Literal["png", "jpg", "webp"]
    width: int = Field(ge=512, le=4096)
    height: int = Field(ge=512, le=4096)
    watermark: bool
    elapsed_seconds: float = Field(ge=0, allow_inf_nan=False)
    usage: dict[str, int] | None = None
    created_at: datetime

    @model_validator(mode="after")
    def validate_url(self):
        if self.url != f"/api/images/{self.image_id}":
            raise ValueError("生成图片只能使用服务端提供的本地图片地址。")
        return self

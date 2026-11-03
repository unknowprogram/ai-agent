"""生成图片的预览与下载；仅按 UUID 读取本地已保存图片。"""

import asyncio
from uuid import UUID

from fastapi import APIRouter
from fastapi.responses import FileResponse

from core.exception_handlers import ERROR_RESPONSES
from storage.generated_images import get_image

router = APIRouter(prefix="/api/images", tags=["图片生成"], responses=ERROR_RESPONSES)
MIME_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}


@router.get("/{image_id}", response_class=FileResponse)
async def read_generated_image(image_id: UUID, download: bool = False):
    image, path = await asyncio.to_thread(get_image, str(image_id))
    return FileResponse(
        path, media_type=MIME_TYPES[image.file_type],
        filename=f"sensenova-{image_id}.{image.file_type}" if download else None,
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=86400"},
    )

"""文件上传与解析结果查询。"""

import asyncio
from uuid import UUID

from fastapi import APIRouter, File, UploadFile, status

from core.exception_handlers import ERROR_RESPONSES
from core.exceptions import FileTooLargeError
from schemas.files import DocumentInfo
from service.files import MAX_UPLOAD_BYTES, ingest_file
from storage import documents


router = APIRouter(
    prefix="/api/files",
    tags=["文件识别"],
    responses=ERROR_RESPONSES,
)


@router.post(
    "",
    response_model=DocumentInfo,
    status_code=status.HTTP_201_CREATED,
)
async def upload_file(file: UploadFile = File(...)):
    try:
        raw = await file.read(MAX_UPLOAD_BYTES + 1)

        if len(raw) > MAX_UPLOAD_BYTES:
            raise FileTooLargeError()

        return await ingest_file(
            file.filename or "未命名文件",
            raw,
        )

    finally:
        await file.close()


@router.get("/{file_id}", response_model=DocumentInfo)
async def get_file(file_id: UUID):
    return await asyncio.to_thread(
        documents.get_document,
        str(file_id),
    )
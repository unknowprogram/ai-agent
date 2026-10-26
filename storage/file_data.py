"""保存表格原文件；数据库继续管理文件元数据和会话关联。"""

import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import UUID

from core.exceptions import FileDataUnavailableError, FileTooLargeError


FILE_DATA_DIR = Path(__file__).resolve().parents[1] / ".data" / "files"
MAX_FILE_BYTES = 20 * 1024 * 1024


def _path(file_id: str) -> Path:
    # 文件名完全由 UUID 决定，不使用上传文件名，避免路径穿越。
    return FILE_DATA_DIR / f"{UUID(file_id)}.bin"


def save_file_data(file_id: str, raw: bytes) -> None:
    if not raw or len(raw) > MAX_FILE_BYTES:
        raise FileTooLargeError()
    target = _path(file_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        # 先写临时文件再原子替换，读取方不会看到只写了一半的文件。
        with NamedTemporaryFile(dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def get_file_data(file_id: str) -> bytes:
    try:
        with _path(file_id).open("rb") as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
    except FileNotFoundError as exc:
        raise FileDataUnavailableError() from exc
    if not raw:
        raise FileDataUnavailableError()
    if len(raw) > MAX_FILE_BYTES:
        raise FileTooLargeError()
    return raw


def remove_file_data(file_id: str) -> None:
    """元数据保存失败时，清理本次创建的原文件。"""
    _path(file_id).unlink(missing_ok=True)

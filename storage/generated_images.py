"""生图原件与本地元数据；会话关联随问答在同一个 MySQL 事务中保存。"""

import json
import os
from contextlib import suppress
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import UUID

from pydantic import ValidationError
from pymysql.err import ProgrammingError

from core.exceptions import (
    GeneratedImageNotFoundError, ImageGenerationSchemaError, ImageGenerationStorageError,
)
from schemas.images import GeneratedImage
from service.images import MAX_IMAGE_BYTES
from storage.mysql import _transaction

IMAGE_DATA_DIR = Path(__file__).resolve().parents[1] / ".data" / "generated"
MAX_METADATA_BYTES = 16384


def _paths(image_id: str) -> tuple[Path, Path]:
    identifier = str(UUID(image_id))
    return IMAGE_DATA_DIR / f"{identifier}.bin", IMAGE_DATA_DIR / f"{identifier}.json"


def _atomic_write(target: Path, content: bytes) -> None:
    temporary = None
    try:
        with NamedTemporaryFile(dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_image(raw: bytes, image: GeneratedImage) -> None:
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ImageGenerationStorageError()
    target, metadata = _paths(str(image.image_id))
    encoded = image.model_dump_json().encode("utf-8")
    if len(encoded) > MAX_METADATA_BYTES:
        raise ImageGenerationStorageError()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or metadata.exists():
            raise ImageGenerationStorageError()
        _atomic_write(target, raw)
        # 元数据最后落盘，读取方只有看到元数据才会提供图片。
        _atomic_write(metadata, encoded)
    except OSError as exc:
        with suppress(OSError):
            target.unlink(missing_ok=True)
        with suppress(OSError):
            metadata.unlink(missing_ok=True)
        raise ImageGenerationStorageError() from exc


def get_image(image_id: str) -> tuple[GeneratedImage, Path]:
    target, metadata = _paths(image_id)
    try:
        with metadata.open("rb") as stream:
            encoded = stream.read(MAX_METADATA_BYTES + 1)
        if len(encoded) > MAX_METADATA_BYTES:
            raise ImageGenerationStorageError()
        image = GeneratedImage.model_validate_json(encoded)
        if str(image.image_id) != str(UUID(image_id)):
            raise ImageGenerationStorageError()
        if not target.is_file() or not 0 < target.stat().st_size <= MAX_IMAGE_BYTES:
            raise GeneratedImageNotFoundError()
        return image, target
    except FileNotFoundError as exc:
        raise GeneratedImageNotFoundError() from exc
    except (OSError, ValidationError) as exc:
        raise ImageGenerationStorageError() from exc


def check_schema() -> None:
    """生成前预检，缺少存储表时先报错，避免生成成功后才发现无法保存会话。"""
    try:
        with _transaction() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT id FROM generated_images LIMIT 0")
    except ProgrammingError as exc:
        if exc.args and exc.args[0] == 1146:
            raise ImageGenerationSchemaError() from exc
        raise


def save_message_images(cursor, conversation_id: str, message_id: int, images: list[dict]) -> None:
    validated = [GeneratedImage.model_validate(item) for item in images]
    if len(validated) > 3 or len({item.image_id for item in validated}) != len(validated):
        raise ValueError("生成图片数量或 ID 无效。")
    try:
        cursor.executemany(
            "INSERT INTO generated_images "
            "(id, conversation_id, message_id, prompt, model, file_type, width, height, "
            "watermark, elapsed_seconds, `usage`) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (str(image.image_id), conversation_id, message_id, image.prompt, image.model,
                 image.file_type, image.width, image.height, image.watermark, image.elapsed_seconds,
                 json.dumps(image.usage) if image.usage is not None else None)
                for image in validated
            ],
        )
    except ProgrammingError as exc:
        if exc.args and exc.args[0] == 1146:
            raise ImageGenerationSchemaError() from exc
        raise


def load_message_images(cursor, messages: list[dict]) -> None:
    by_id = {item["id"]: item for item in messages}
    for message in messages:
        message["generated_images"] = []
    if not by_id:
        return
    placeholders = ",".join(["%s"] * len(by_id))
    try:
        cursor.execute(
            "SELECT id, message_id, prompt, model, file_type, width, height, watermark, "
            f"elapsed_seconds, `usage`, created_at FROM generated_images WHERE message_id IN ({placeholders}) "
            "ORDER BY created_at, id",
            tuple(by_id),
        )
    except ProgrammingError as exc:
        # 升级后尚未补建生图表时，已有普通会话仍可读取。
        if exc.args and exc.args[0] == 1146:
            return
        raise
    for row in cursor.fetchall():
        image_id = row["id"]
        image = GeneratedImage(
            image_id=image_id, url=f"/api/images/{image_id}",
            prompt=row["prompt"], model=row["model"], file_type=row["file_type"],
            width=row["width"], height=row["height"], watermark=row["watermark"],
            elapsed_seconds=row["elapsed_seconds"],
            usage=json.loads(row["usage"]) if row["usage"] else None, created_at=row["created_at"],
        )
        by_id[row["message_id"]]["generated_images"].append(image.model_dump(mode="json"))

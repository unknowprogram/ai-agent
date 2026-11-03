"""可丢弃的摘要缓存：按文件 UUID 和内容指纹隔离，失败时回到正常处理。"""

import json
import logging
import os
import re
import time
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import UUID

CACHE_DIR = Path(__file__).resolve().parents[1] / ".data" / "summary_cache"
CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_CACHE_BYTES = 4 * 1024 * 1024
MAX_ENTRIES_PER_FILE = 8
logger = logging.getLogger(__name__)


def _path(file_id: str, fingerprint: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ValueError("无效摘要指纹。")
    return CACHE_DIR / str(UUID(file_id)) / f"{fingerprint}.json"


def read(file_id: str, fingerprint: str) -> dict | None:
    try:
        target = _path(file_id, fingerprint)
        if time.time() - target.stat().st_mtime > CACHE_TTL_SECONDS:
            return None
        with target.open("rb") as stream:
            raw = stream.read(MAX_CACHE_BYTES + 1)
        if len(raw) > MAX_CACHE_BYTES:
            return None
        result = json.loads(raw)
        return result if isinstance(result, dict) else None
    except (OSError, ValueError):
        return None


def write(file_id: str, fingerprint: str, data: dict) -> None:
    temporary = None
    try:
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_CACHE_BYTES:
            return
        target = _path(file_id, fingerprint)
        target.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=target.parent, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        entries = [path for path in target.parent.glob("*.json") if re.fullmatch(r"[0-9a-f]{64}", path.stem)]
        for stale in sorted(entries, key=lambda path: path.stat().st_mtime, reverse=True)[MAX_ENTRIES_PER_FILE:]:
            stale.unlink(missing_ok=True)
    except (OSError, ValueError):
        # 缓存不可用不影响摘要与问答保存，也不会伪造模型缓存命中。
        logger.debug("summary cache write unavailable", exc_info=True)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

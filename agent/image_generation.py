"""SenseNova U1.5 Lite 文生图：独立的 Images API，同步请求且不自动重试。"""

import base64
import binascii
import json
import os
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from urllib.parse import urlsplit, urlunsplit

import httpx
from dotenv import load_dotenv
from sympy import false

from core.exceptions import (
    ImageGenerationAuthenticationError, ImageGenerationConfigError, ImageGenerationError,
    ImageGenerationRateLimitError, ImageGenerationRejectedError,
    ImageGenerationResponseError, ImageGenerationTimeoutError,
)
from schemas.images import validate_image_size
from service.images import MAX_IMAGE_BYTES

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

# Base64 的上界加少量 JSON 元数据，防止上游异常响应无限占用内存。
MAX_RESPONSE_BYTES = 4 * ((MAX_IMAGE_BYTES + 2) // 3) + 65536


@dataclass(frozen=True)
class ImageGenerationSettings:
    api_key: str
    base_url: str
    model: str
    timeout: float
    watermark: bool
    prompt_extend: bool


@dataclass(frozen=True)
class ImageGenerationResult:
    raw: bytes
    prompt: str
    model: str
    watermark: bool
    usage: dict[str, int] | None
    elapsed_seconds: float


def _boolean(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
    if value in {"true", "1", "yes", "on"}:
        return True
    if value in {"false", "0", "no", "off"}:
        return False
    raise ImageGenerationConfigError()


def _settings() -> ImageGenerationSettings:
    api_key = (
        os.getenv("SENSENOVA_IMAGE_API_KEY", "").strip()
        or os.getenv("SENSENOVA_API_KEY", "").strip()
    )
    base_url = os.getenv(
        "SENSENOVA_IMAGE_BASE_URL", "https://token.sensenova.cn/v1"
    ).strip()
    model = os.getenv("SENSENOVA_IMAGE_MODEL", "sensenova-u1.5-lite").strip()
    try:
        parts = urlsplit(base_url)
        timeout = float(os.getenv("SENSENOVA_IMAGE_TIMEOUT", "300"))
        if (
            not api_key or not model or len(model) > 128
            or parts.scheme not in {"http", "https"} or not parts.netloc
            or parts.username or parts.password or parts.query or parts.fragment
            or not 1 <= timeout <= 600
        ):
            raise ValueError("Invalid image generation configuration")
        path = parts.path.rstrip("/")
        for endpoint in ("/images/generations", "/chat/completions"):
            if path.endswith(endpoint):
                path = path[:-len(endpoint)]
                break
        base_url = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    except ValueError as exc:
        raise ImageGenerationConfigError() from exc
    return ImageGenerationSettings(
        api_key, base_url, model, timeout,
        _boolean("SENSENOVA_IMAGE_WATERMARK", false),
        _boolean("SENSENOVA_IMAGE_PROMPT_EXTEND", True),
    )


def _check_status(status: int) -> None:
    if status in {401, 403}:
        raise ImageGenerationAuthenticationError()
    if status == 429:
        raise ImageGenerationRateLimitError()
    if status in {408, 504}:
        raise ImageGenerationTimeoutError()
    if status in {400, 422}:
        raise ImageGenerationRejectedError()
    if not 200 <= status < 300:
        raise ImageGenerationError()


def _decode(data: object) -> tuple[bytes, dict[str, int] | None]:
    if not isinstance(data, dict):
        raise ImageGenerationResponseError()
    images = data.get("data")
    if (
        not isinstance(images, list) or len(images) != 1
        or not isinstance(images[0], dict)
        or not isinstance(images[0].get("b64_json"), str)
        or images[0].get("url")
    ):
        raise ImageGenerationResponseError()
    encoded = images[0]["b64_json"]
    if len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        raise ImageGenerationResponseError()
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ImageGenerationResponseError() from exc
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ImageGenerationResponseError()
    usage = data.get("usage")
    counts = {
        key: value for key, value in (usage.items() if isinstance(usage, dict) else [])
        if key in {"input_tokens", "output_tokens", "total_tokens", "images_count"}
        and type(value) is int and value >= 0
    }
    return raw, counts or None


async def generate_image_bytes(prompt: str, size: str = "1024x1024") -> ImageGenerationResult:
    prompt = prompt.strip()
    if not prompt or len(prompt) > 2000:
        raise ValueError("生成描述不能为空，且不能超过 2000 字符。")
    size = validate_image_size(size)
    settings = _settings()
    payload = {
        "model": settings.model, "prompt": prompt, "n": 1, "size": size,
        "output_format": "png", "response_format": "b64_json",
        "watermark": settings.watermark, "prompt_extend": settings.prompt_extend,
    }
    started = perf_counter()
    try:
        # httpx 默认不重试创建请求。取消等待会关闭连接，但不承诺撤销上游已接受的生成。
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(settings.timeout, connect=min(10, settings.timeout)),
            follow_redirects=False,
        ) as client:
            async with client.stream(
                "POST", f"{settings.base_url}/images/generations",
                headers={"Authorization": f"Bearer {settings.api_key}", "Accept": "application/json"},
                json=payload,
            ) as response:
                _check_status(response.status_code)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise ImageGenerationResponseError()
                    body.extend(chunk)
        raw, usage = _decode(json.loads(body))
    except httpx.TimeoutException as exc:
        raise ImageGenerationTimeoutError() from exc
    except httpx.RequestError as exc:
        raise ImageGenerationError() from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ImageGenerationResponseError() from exc
    return ImageGenerationResult(
        raw, prompt, settings.model, settings.watermark, usage,
        round(perf_counter() - started, 2),
    )

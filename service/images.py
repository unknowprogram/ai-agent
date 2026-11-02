"""校验和预处理图片；全部操作在内存中完成。"""

import base64
import warnings
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

from core.exceptions import FileParseError, FileTooLargeError, UnsupportedFileError

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
IMAGE_TYPES = {"jpg", "jpeg", "png", "webp"}

# 以下是本项目的资源限制，不代表 SenseNova 的官方限制。
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 24_000_000
MAX_SENT_SIDE = 2048

FORMAT_INFO = {
    "JPEG": ("jpg", "image/jpeg"),
    "PNG": ("png", "image/png"),
    "WEBP": ("webp", "image/webp"),
}


@dataclass(frozen=True)
class ImageInfo:
    file_type: str
    mime_type: str
    width: int
    height: int


@dataclass(frozen=True)
class PreparedImage:
    # data_url 仅在视觉模型请求中使用，不写入工具结果或会话历史。
    data_url: str
    original_width: int
    original_height: int
    sent_width: int
    sent_height: int
    resized: bool


def _open_checked(raw: bytes) -> Image.Image:
    if not raw:
        raise FileParseError()
    if len(raw) > MAX_IMAGE_BYTES:
        raise FileTooLargeError()

    image = None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(BytesIO(raw))

            if image.format not in FORMAT_INFO:
                raise UnsupportedFileError()
            if getattr(image, "n_frames", 1) != 1:
                # 此版本只接收静态图片，避免悄悄忽略后续帧。
                raise UnsupportedFileError()

            width, height = image.size
            if width <= 0 or height <= 0:
                raise FileParseError()
            if width * height > MAX_IMAGE_PIXELS:
                raise FileTooLargeError()

            # 完整解码，避免仅根据扩展名或文件头接受损坏图片。
            image.load()
        return image

    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        if image is not None:
            image.close()
        raise FileTooLargeError() from exc
    except (UnsupportedFileError, FileTooLargeError, FileParseError):
        if image is not None:
            image.close()
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        if image is not None:
            image.close()
        raise FileParseError() from exc


def inspect_image(raw: bytes) -> ImageInfo:
    """上传阶段校验实际图片格式并读取尺寸，不调用大模型。"""
    with _open_checked(raw) as image:
        file_type, mime_type = FORMAT_INFO[image.format]
        return ImageInfo(file_type, mime_type, image.width, image.height)


def prepare_image(raw: bytes) -> PreparedImage:
    """校正 EXIF 方向、按比例缩小并转为 JPEG Data URL。"""
    with _open_checked(raw) as original:
        original_width, original_height = original.size

        with ImageOps.exif_transpose(original) as oriented:
            oriented_size = oriented.size
            oriented.thumbnail((MAX_SENT_SIDE, MAX_SENT_SIDE), Image.Resampling.LANCZOS)
            sent_width, sent_height = oriented.size

            # 透明区域铺白底，避免转为 JPEG 后背景变黑。
            with oriented.convert("RGBA") as rgba:
                with Image.new("RGB", rgba.size, "white") as rgb:
                    with rgba.getchannel("A") as alpha:
                        rgb.paste(rgba, mask=alpha)
                    buffer = BytesIO()
                    rgb.save(buffer, format="JPEG", quality=92, optimize=True)
                    encoded = buffer.getvalue()

    if len(encoded) > MAX_IMAGE_BYTES:
        raise FileTooLargeError()

    data_url = "data:image/jpeg;base64," + base64.b64encode(encoded).decode("ascii")
    return PreparedImage(
        data_url=data_url,
        original_width=original_width,
        original_height=original_height,
        sent_width=sent_width,
        sent_height=sent_height,
        resized=(sent_width, sent_height) != oriented_size,
    )

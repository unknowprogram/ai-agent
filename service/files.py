"""解析上传文件，并将提取内容分块保存。"""

import asyncio
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader

from core.exceptions import (
    FileParseError,
    FileTextUnavailableError,
    FileTooLargeError,
    UnsupportedFileError,
)
from storage import documents


MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_DOCUMENT_CHARS = 200_000
MAX_PDF_PAGES = 200
MAX_CHUNKS = 400

CHUNK_SIZE = 1000
CHUNK_OVERLAP = 150

SUPPORTED_SUFFIXES = {".txt", ".md", ".pdf", ".docx"}


def _decode_text(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")

    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = raw.decode(encoding)

            if "\x00" in text:
                raise FileParseError()

            return text
        except UnicodeDecodeError:
            continue

    raise FileParseError()


def _read_pdf(raw: bytes) -> tuple[list[tuple[str, str]], list[str]]:
    if b"%PDF-" not in raw[:1024]:
        raise FileParseError()

    reader = PdfReader(BytesIO(raw), strict=False)

    if reader.is_encrypted and not reader.decrypt(""):
        raise FileParseError()

    if len(reader.pages) > MAX_PDF_PAGES:
        raise FileTooLargeError()

    sections = []
    warnings = []
    char_count = 0

    for page_number, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()

        if not text:
            warnings.append(
                f"第 {page_number} 页未提取到文字，未进行 OCR。"
            )
            continue

        char_count += len(text)

        if char_count > MAX_DOCUMENT_CHARS:
            raise FileTooLargeError()

        sections.append((f"第 {page_number} 页", text))

    return sections, warnings


def _read_docx(raw: bytes) -> list[tuple[str, str]]:
    # 控制 DOCX 解压后的规模。
    with ZipFile(BytesIO(raw)) as archive:
        members = archive.infolist()

        if len(members) > 2000:
            raise FileTooLargeError()

        if sum(item.file_size for item in members) > 50 * 1024 * 1024:
            raise FileTooLargeError()

    document = Document(BytesIO(raw))
    blocks = []
    char_count = 0

    # 按文档中的实际顺序读取段落和表格。
    for block in document.iter_inner_content():
        if isinstance(block, Paragraph):
            text = block.text.strip()

        elif isinstance(block, Table):
            text = "\n".join(
                " | ".join(
                    cell.text.replace("\n", " ").strip()
                    for cell in row.cells
                )
                for row in block.rows
            ).strip()

        else:
            continue

        if not text:
            continue

        char_count += len(text)

        if char_count > MAX_DOCUMENT_CHARS:
            raise FileTooLargeError()

        blocks.append(text)

    # DOCX 未经过排版渲染，不能可靠提供页码。
    return [("正文", "\n\n".join(blocks))]


def _split_sections(
    sections: list[tuple[str, str]],
) -> list[dict]:
    chunks = []

    for location, text in sections:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        start = 0

        while start < len(text):
            end = min(start + CHUNK_SIZE, len(text))

            # 尽量在换行处切分。
            if end < len(text):
                newline = text.rfind(
                    "\n",
                    start + CHUNK_SIZE // 2,
                    end,
                )

                if newline != -1:
                    end = newline + 1

            content = text[start:end].strip()

            if content:
                chunks.append(
                    {
                        "location": location,
                        "content": content,
                    }
                )

            if len(chunks) > MAX_CHUNKS:
                raise FileTooLargeError()

            if end >= len(text):
                break

            start = max(start + 1, end - CHUNK_OVERLAP)

    return chunks


def parse_file(
    filename: str,
    raw: bytes,
) -> tuple[str, list[dict], int, list[str]]:
    suffix = Path(filename).suffix.lower()

    if suffix not in SUPPORTED_SUFFIXES:
        raise UnsupportedFileError()

    if len(raw) > MAX_UPLOAD_BYTES:
        raise FileTooLargeError()

    if not raw:
        raise FileTextUnavailableError()

    try:
        warnings = []

        if suffix in {".txt", ".md"}:
            sections = [("正文", _decode_text(raw))]

        elif suffix == ".pdf":
            sections, warnings = _read_pdf(raw)

        else:
            sections = _read_docx(raw)

        char_count = sum(len(text) for _, text in sections)

        if char_count > MAX_DOCUMENT_CHARS:
            raise FileTooLargeError()

        chunks = _split_sections(sections)

        if not chunks:
            raise FileTextUnavailableError()

        return suffix.lstrip("."), chunks, char_count, warnings

    except (
        UnsupportedFileError,
        FileTooLargeError,
        FileTextUnavailableError,
        FileParseError,
    ):
        raise

    except Exception as exc:
        raise FileParseError() from exc


async def ingest_file(filename: str, raw: bytes) -> dict:
    filename = filename.replace("\\", "/").split("/")[-1]
    filename = filename.strip() or "未命名文件"
    if len(filename) > 255:
        suffix = Path(filename).suffix
        filename = filename[:255 - len(suffix)] + suffix

    file_type, chunks, char_count, warnings = await asyncio.to_thread(
        parse_file,
        filename,
        raw,
    )

    file_id = await asyncio.to_thread(
        documents.save_document,
        filename,
        file_type,
        char_count,
        chunks,
        warnings,
    )

    return await asyncio.to_thread(
        documents.get_document,
        file_id,
    )

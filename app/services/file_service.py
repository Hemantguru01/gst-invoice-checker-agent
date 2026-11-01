"""Upload validation and safe on-disk storage.

Uploaded files are never executed. They are identified by magic bytes (not by the
client-supplied extension or content type), fully decoded once to reject corrupted
files, and stored under a server-generated UUID name inside the upload directory.
"""

from __future__ import annotations

import io
import re
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePath

import pymupdf as fitz
from PIL import Image

from app.core.exceptions import FileTooLargeError, InvalidFileError, UnsupportedFileTypeError

Image.MAX_IMAGE_PIXELS = 60_000_000  # guard against decompression bombs

ALLOWED_EXTENSIONS = {".pdf": "pdf", ".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg"}
CONTENT_TYPES = {"pdf": "application/pdf", "png": "image/png", "jpeg": "image/jpeg"}


@dataclass
class ValidatedFile:
    kind: str  # pdf | png | jpeg
    content_type: str
    safe_name: str
    size: int


def sanitize_filename(name: str | None) -> str:
    """Keep only a display-safe basename; it is never used as a filesystem path."""
    base = PurePath((name or "invoice").replace("\\", "/")).name
    base = re.sub(r"[^A-Za-z0-9._\- ]", "_", base).strip(" .") or "invoice"
    return base[:120]


def detect_kind(content: bytes) -> str | None:
    if content.startswith(b"%PDF-"):
        return "pdf"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if content.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    return None


def validate_upload(filename: str | None, content: bytes, max_bytes: int, max_pdf_pages: int = 50) -> ValidatedFile:
    safe_name = sanitize_filename(filename)
    ext = Path(safe_name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise UnsupportedFileTypeError(
            f"Unsupported file type '{ext or 'none'}'. Upload a PDF, PNG or JPG invoice."
        )
    if not content:
        raise InvalidFileError("The uploaded file is empty.")
    if len(content) > max_bytes:
        raise FileTooLargeError(
            f"File is {len(content) / 1_048_576:.1f} MB; the limit is {max_bytes / 1_048_576:.0f} MB."
        )

    kind = detect_kind(content)
    if kind is None:
        raise InvalidFileError("The file content is not a valid PDF, PNG or JPEG.")
    if kind != ALLOWED_EXTENSIONS[ext]:
        raise InvalidFileError(f"The file extension '{ext}' does not match its content ({kind}).")

    if kind == "pdf":
        _check_pdf(content, max_pdf_pages)
    else:
        _check_image(content)
    return ValidatedFile(kind, CONTENT_TYPES[kind], safe_name, len(content))


def _check_pdf(content: bytes, max_pages: int) -> None:
    try:
        doc = fitz.open(stream=content, filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        raise InvalidFileError("The PDF is corrupted or unreadable.") from exc
    with doc:
        if doc.needs_pass:
            raise InvalidFileError("Password-protected PDFs are not supported.")
        if doc.page_count == 0:
            raise InvalidFileError("The PDF has no pages.")
        if doc.page_count > max_pages:
            raise InvalidFileError(f"The PDF has {doc.page_count} pages; the limit is {max_pages}.")
        try:
            doc[0].get_text("text")
        except Exception as exc:  # noqa: BLE001
            raise InvalidFileError("The PDF is corrupted or unreadable.") from exc


def _check_image(content: bytes) -> None:
    try:
        with Image.open(io.BytesIO(content)) as img:
            img.verify()
        with Image.open(io.BytesIO(content)) as img:
            img.load()
    except Image.DecompressionBombError as exc:
        raise InvalidFileError("The image is too large to process.") from exc
    except Exception as exc:  # noqa: BLE001
        raise InvalidFileError("The image is corrupted or unreadable.") from exc


class FileStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, invoice_id: str, kind: str, content: bytes) -> Path:
        ext = ".pdf" if kind == "pdf" else (".png" if kind == "png" else ".jpg")
        path = self.path_for(invoice_id, ext)
        path.write_bytes(content)
        return path

    def path_for(self, invoice_id: str, ext: str) -> Path:
        uuid.UUID(invoice_id)  # only server-generated IDs are accepted
        path = (self.root / f"{invoice_id}{ext}").resolve()
        if path.parent != self.root:
            raise InvalidFileError("Unsafe file path.")
        return path

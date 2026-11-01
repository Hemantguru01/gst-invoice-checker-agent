"""Turns an uploaded PDF or image into text lines (text layer first, OCR as fallback)."""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field

import pymupdf as fitz
from PIL import Image

from app.core.exceptions import ExtractionError
from app.services.ocr_service import OCREngine, TextLine, TextSegment, ocr_image, segments_to_lines

logger = logging.getLogger(__name__)

MIN_TEXT_LAYER_CHARS = 40


@dataclass
class DocumentText:
    lines: list[TextLine]
    method: str  # pdf_text | ocr | mixed
    page_count: int
    ocr_engine: str | None = None
    warnings: list[str] = field(default_factory=list)
    ocr_pages: list[int] = field(default_factory=list)  # 1-based pages whose text came from OCR

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


MAX_RENDER_PIXELS = 40_000_000


def read_document(content: bytes, kind: str, engine: OCREngine | None, *, dpi: int = 200,
                  max_pages: int = 10, max_render_pixels: int = MAX_RENDER_PIXELS) -> DocumentText:
    if kind == "pdf":
        return _read_pdf(content, engine, dpi, max_pages, max_render_pixels)
    return _read_image(content, engine)


def _render_dpi(page: "fitz.Page", dpi: int, max_pixels: int) -> int:
    """Lower the DPI for oversized pages so one page cannot allocate an enormous bitmap."""
    width_in, height_in = page.rect.width / 72, page.rect.height / 72
    pixels = width_in * dpi * height_in * dpi
    if pixels <= max_pixels:
        return dpi
    return max(36, int(dpi * (max_pixels / pixels) ** 0.5))


def _read_pdf(content: bytes, engine: OCREngine | None, dpi: int, max_pages: int,
              max_render_pixels: int = MAX_RENDER_PIXELS) -> DocumentText:
    try:
        doc = fitz.open(stream=content, filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        raise ExtractionError("The PDF could not be opened; it may be corrupted.") from exc

    with doc:
        warnings: list[str] = []
        if doc.page_count > max_pages:
            warnings.append(f"Only the first {max_pages} of {doc.page_count} pages were processed.")
        lines: list[TextLine] = []
        methods: set[str] = set()
        used_engine: str | None = None
        ocr_pages: list[int] = []
        for page_no, page in enumerate(list(doc)[:max_pages], start=1):
            words = page.get_text("words")
            segments = [TextSegment(w[4], w[0], w[1], w[2], w[3]) for w in words]
            page_lines = segments_to_lines(segments, column_gap=8.0)
            if sum(len(line.text) for line in page_lines) >= MIN_TEXT_LAYER_CHARS:
                methods.add("pdf_text")
                lines.extend(page_lines)
                continue
            # Scanned page: render and OCR it.
            render_dpi = _render_dpi(page, dpi, max_render_pixels)
            if render_dpi < dpi:
                warnings.append(f"Page {page_no} is very large; it was rendered at {render_dpi} dpi for OCR.")
            pix = page.get_pixmap(dpi=render_dpi)
            image = Image.open(io.BytesIO(pix.tobytes("png")))
            lines.extend(ocr_image(image, engine))
            methods.add("ocr")
            ocr_pages.append(page_no)
            used_engine = engine.name if engine else None
        method = "mixed" if len(methods) > 1 else (methods.pop() if methods else "pdf_text")
        return DocumentText(lines, method, doc.page_count, used_engine, warnings, ocr_pages)


def _read_image(content: bytes, engine: OCREngine | None) -> DocumentText:
    try:
        image = Image.open(io.BytesIO(content))
        image.load()
    except Exception as exc:  # noqa: BLE001
        raise ExtractionError("The image could not be decoded; it may be corrupted.") from exc
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    # Small images OCR poorly; upscale modestly.
    if image.width < 1200:
        factor = 1200 / image.width
        image = image.resize((1200, int(image.height * factor)))
    lines = ocr_image(image, engine)
    return DocumentText(lines, "ocr", 1, engine.name if engine else None, ocr_pages=[1])

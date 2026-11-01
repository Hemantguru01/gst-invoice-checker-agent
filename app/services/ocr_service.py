"""OCR engines behind a small common interface.

RapidOCR (ONNX runtime, pip-installable, no system binaries) is preferred.
Tesseract is used when RapidOCR is unavailable and the tesseract binary is installed.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

from PIL import Image

from app.core.exceptions import ExtractionError, OCRUnavailableError

logger = logging.getLogger(__name__)


@dataclass
class TextSegment:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    confidence: float = 1.0


@dataclass
class TextLine:
    text: str
    confidence: float = 1.0


class OCREngine(Protocol):
    name: str

    def recognize(self, image: Image.Image) -> list[TextSegment]: ...


class RapidOCREngine:
    name = "rapidocr"

    def __init__(self) -> None:
        from rapidocr_onnxruntime import RapidOCR  # imported lazily: heavy

        self._engine = RapidOCR()

    def recognize(self, image: Image.Image) -> list[TextSegment]:
        import numpy as np

        result, _ = self._engine(np.array(image.convert("RGB")))
        segments: list[TextSegment] = []
        for box, text, score in result or []:
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            segments.append(TextSegment(str(text), min(xs), min(ys), max(xs), max(ys), float(score)))
        return segments


class TesseractEngine:
    name = "tesseract"

    def __init__(self, cmd: str | None = None) -> None:
        import pytesseract

        if cmd:
            pytesseract.pytesseract.tesseract_cmd = cmd
        pytesseract.get_tesseract_version()  # raises if the binary is missing
        self._pt = pytesseract

    def recognize(self, image: Image.Image) -> list[TextSegment]:
        data = self._pt.image_to_data(image.convert("RGB"), output_type=self._pt.Output.DICT)
        segments = []
        for i, word in enumerate(data["text"]):
            if not word.strip():
                continue
            conf = float(data["conf"][i])
            x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
            segments.append(TextSegment(word, x, y, x + w, y + h, max(conf, 0) / 100))
        return segments


@lru_cache
def _build_engine(preference: str, tesseract_cmd: str | None) -> OCREngine | None:
    order = {"rapidocr": ["rapidocr"], "tesseract": ["tesseract"]}.get(preference, ["rapidocr", "tesseract"])
    for name in order:
        try:
            if name == "rapidocr":
                return RapidOCREngine()
            if tesseract_cmd or shutil.which("tesseract"):
                return TesseractEngine(tesseract_cmd)
        except Exception as exc:  # noqa: BLE001 - any import/runtime failure means "unavailable"
            logger.warning("OCR engine %s unavailable: %s", name, type(exc).__name__)
    return None


def get_ocr_engine(preference: str = "auto", tesseract_cmd: str | None = None) -> OCREngine | None:
    return _build_engine(preference.lower(), tesseract_cmd)


def segments_to_lines(segments: list[TextSegment], column_gap: float) -> list[TextLine]:
    """Group positioned text segments into reading-order lines.

    Segments whose vertical centres are close are put on one line. A horizontal gap
    larger than ``column_gap`` becomes a double space so table columns stay separable.
    """
    if not segments:
        return []
    ordered = sorted(segments, key=lambda s: ((s.y0 + s.y1) / 2, s.x0))
    rows: list[list[TextSegment]] = []
    for seg in ordered:
        centre = (seg.y0 + seg.y1) / 2
        height = max(seg.y1 - seg.y0, 1)
        if rows:
            last = rows[-1]
            last_centre = sum((s.y0 + s.y1) / 2 for s in last) / len(last)
            if abs(centre - last_centre) <= height * 0.5:
                last.append(seg)
                continue
        rows.append([seg])

    lines: list[TextLine] = []
    for row in rows:
        row.sort(key=lambda s: s.x0)
        parts = [row[0].text]
        for prev, cur in zip(row, row[1:]):
            parts.append("  " if cur.x0 - prev.x1 > column_gap else " ")
            parts.append(cur.text)
        text = "".join(parts).strip()
        if text:
            lines.append(TextLine(text, min(s.confidence for s in row)))
    return lines


def ocr_image(image: Image.Image, engine: OCREngine | None) -> list[TextLine]:
    if engine is None:
        raise OCRUnavailableError(
            "No OCR engine is available. Install 'rapidocr_onnxruntime' (recommended) or "
            "Tesseract to read scanned invoices and images."
        )
    try:
        segments = engine.recognize(image)
    except Exception as exc:  # noqa: BLE001
        raise ExtractionError(f"OCR failed ({engine.name}): {type(exc).__name__}") from exc
    for seg in segments:
        seg.text = restore_word_spaces(seg.text)
    # OCR engines return phrases; treat any visible gap as a column break.
    return segments_to_lines(segments, column_gap=image.width * 0.012)


_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z])(?=[A-Z])")


def restore_word_spaces(text: str) -> str:
    """Some OCR models drop spaces ('TaxableValue', 'BillTo:'); split at lower→upper boundaries."""
    return _CAMEL_BOUNDARY.sub(" ", text)

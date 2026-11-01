"""Extraction pipeline: document -> text lines -> structured invoice data."""

from __future__ import annotations

import logging

from app.agent.llm_client import LLMClient
from app.agent.llm_extraction import fill_missing_fields
from app.core.exceptions import ExtractionError
from app.models.invoice import (
    TRACKED_FIELDS,
    ExtractionResult,
    ExtractionStatus,
    FieldSource,
    InvoiceData,
)
from app.services.document_reader import read_document
from app.services.invoice_parser import InvoiceParser
from app.services.ocr_service import OCREngine
from app.services.reference_data import StateCodes, get_state_codes
from app.validators.fields import MANDATORY

logger = logging.getLogger(__name__)

TAX_HEADS = ("cgst", "sgst", "igst")


def missing_fields(invoice: InvoiceData) -> list[str]:
    missing = []
    for name in TRACKED_FIELDS:
        if name in TAX_HEADS:
            continue
        value = getattr(invoice, name)
        if value is None or (name == "line_items" and not value):
            missing.append(name)
    if all(getattr(invoice, h) is None for h in TAX_HEADS):
        missing.append("tax_amounts (CGST/SGST/IGST)")
    return missing


class ExtractionService:
    def __init__(self, ocr_engine: OCREngine | None, llm: LLMClient | None = None,
                 state_codes: StateCodes | None = None, *, dpi: int = 200, max_pages: int = 10,
                 llm_assisted: bool = True, max_render_pixels: int = 40_000_000) -> None:
        self.ocr_engine = ocr_engine
        self.llm = llm if llm_assisted else None
        self.state_codes = state_codes or get_state_codes()
        self.dpi = dpi
        self.max_pages = max_pages
        self.max_render_pixels = max_render_pixels

    def extract(self, content: bytes, kind: str) -> ExtractionResult:
        try:
            document = read_document(content, kind, self.ocr_engine, dpi=self.dpi, max_pages=self.max_pages,
                                     max_render_pixels=self.max_render_pixels)
        except ExtractionError as exc:
            logger.info("Extraction failed: %s", exc.code)
            return ExtractionResult(status=ExtractionStatus.FAILED, method="none", error=exc.message,
                                    missing_fields=list(TRACKED_FIELDS))

        text = document.text
        if not text.strip():
            return ExtractionResult(
                status=ExtractionStatus.FAILED, method=document.method, ocr_engine=document.ocr_engine,
                page_count=document.page_count, ocr_pages=document.ocr_pages, missing_fields=list(TRACKED_FIELDS),
                error="No readable text was found in the document.",
            )

        # One parser per document: it holds per-document state and extractions run in parallel threads.
        parsed = InvoiceParser(self.state_codes).parse(document.lines)
        invoice = parsed.invoice
        # Mixed documents cannot be mapped field by field, so their values are treated as OCR (less certain).
        source = FieldSource.PDF_TEXT if document.method == "pdf_text" else FieldSource.OCR
        sources = {f: source for f in TRACKED_FIELDS
                   if getattr(invoice, f) not in (None, [])}
        uncertain = set(parsed.uncertain_fields)
        warnings = list(document.warnings) + parsed.warnings

        if self.llm is not None:
            filled, rejected = fill_missing_fields(invoice, text, self.llm)
            for f in filled:
                sources[f] = FieldSource.LLM_GROUNDED
                uncertain.add(f)
            if filled:
                warnings.append(f"AI-assisted extraction filled {len(filled)} field(s); verify them.")
            if rejected:
                warnings.append(f"Discarded {len(rejected)} AI-suggested value(s) not found in the document text.")
            if invoice.place_of_supply and not invoice.place_of_supply_code:
                invoice.place_of_supply_code = self.state_codes.code_for_name(invoice.place_of_supply)

        missing = missing_fields(invoice)
        mandatory_missing = [f for f in MANDATORY if f in missing]
        status = ExtractionStatus.PARTIAL if mandatory_missing else ExtractionStatus.SUCCESS
        return ExtractionResult(
            status=status,
            method=document.method,
            ocr_engine=document.ocr_engine,
            invoice=invoice,
            missing_fields=missing,
            uncertain_fields=sorted(uncertain),
            field_sources=sources,
            warnings=warnings,
            raw_text_chars=len(text),
            page_count=document.page_count,
            ocr_pages=document.ocr_pages,
        )

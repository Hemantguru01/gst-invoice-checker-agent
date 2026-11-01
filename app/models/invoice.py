"""Structured invoice data produced by the extraction layer."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field


class FieldSource(str, Enum):
    PDF_TEXT = "pdf_text"
    OCR = "ocr"
    LLM_GROUNDED = "llm_grounded"  # suggested by LLM and found verbatim in the document text
    USER = "user"


class LineItem(BaseModel):
    description: str | None = None
    hsn_sac: str | None = None
    quantity: Decimal | None = None
    unit: str | None = None
    unit_price: Decimal | None = None
    taxable_value: Decimal | None = None
    gst_rate: Decimal | None = Field(default=None, description="Rate (%) printed on the invoice line, if any")


class InvoiceData(BaseModel):
    """Every field is optional: absent values stay None, they are never guessed."""

    supplier_name: str | None = None
    supplier_gstin: str | None = None
    recipient_name: str | None = None
    recipient_gstin: str | None = None
    invoice_number: str | None = None
    invoice_date: date | None = None
    invoice_date_raw: str | None = None
    place_of_supply: str | None = None
    place_of_supply_code: str | None = None
    line_items: list[LineItem] = Field(default_factory=list)
    taxable_value: Decimal | None = None
    cgst: Decimal | None = None
    sgst: Decimal | None = None
    igst: Decimal | None = None
    round_off: Decimal | None = None
    total_invoice_value: Decimal | None = None


TRACKED_FIELDS: tuple[str, ...] = (
    "supplier_name",
    "supplier_gstin",
    "recipient_name",
    "recipient_gstin",
    "invoice_number",
    "invoice_date",
    "place_of_supply",
    "line_items",
    "taxable_value",
    "cgst",
    "sgst",
    "igst",
    "total_invoice_value",
)


class ExtractionStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class ExtractionResult(BaseModel):
    status: ExtractionStatus
    method: str = Field(description="pdf_text, ocr, or mixed")
    ocr_engine: str | None = None
    invoice: InvoiceData = Field(default_factory=InvoiceData)
    missing_fields: list[str] = Field(default_factory=list)
    uncertain_fields: list[str] = Field(default_factory=list)
    field_sources: dict[str, FieldSource] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    raw_text_chars: int = 0
    page_count: int = 0
    error: str | None = None

"""Structured invoice data produced by the extraction layer."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field, computed_field, model_validator

from app.utils.invoice_number import invoice_number_key, normalize_invoice_number


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
    discount: Decimal | None = Field(default=None, description="Line discount amount, if a discount column is printed")
    taxable_value: Decimal | None = None
    gst_rate: Decimal | None = Field(default=None, description="Rate (%) printed on the invoice line, if any")


class InvoiceData(BaseModel):
    """Every field is optional: absent values stay None, they are never guessed."""

    supplier_name: str | None = None
    supplier_gstin: str | None = None
    recipient_name: str | None = None
    recipient_gstin: str | None = None
    invoice_number: str | None = Field(
        default=None, description="Invoice number with whitespace tidied around separators (display form)")
    invoice_number_raw: str | None = Field(
        default=None, description="Invoice number exactly as extracted or entered; kept for audit")
    invoice_date: date | None = None
    invoice_date_raw: str | None = None
    place_of_supply: str | None = None
    place_of_supply_code: str | None = None
    line_items: list[LineItem] = Field(default_factory=list)
    supply_indicators: list[str] = Field(
        default_factory=list, description="Export/SEZ/LUT/deemed-export wording found on the invoice")
    discount: Decimal | None = Field(default=None, description="Invoice-level discount printed on the invoice")
    taxable_value: Decimal | None = None
    cgst: Decimal | None = None
    sgst: Decimal | None = None
    igst: Decimal | None = None
    round_off: Decimal | None = None
    total_invoice_value: Decimal | None = None

    @model_validator(mode="after")
    def _keep_raw_invoice_number(self) -> "InvoiceData":
        if self.invoice_number is not None:
            if self.invoice_number_raw is None or normalize_invoice_number(self.invoice_number_raw) != \
                    normalize_invoice_number(self.invoice_number):
                self.invoice_number_raw = self.invoice_number  # a new value (e.g. a correction) is its own original
            self.invoice_number = normalize_invoice_number(self.invoice_number)
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def invoice_number_key(self) -> str | None:
        """Comparison value for duplicate detection (see app.utils.invoice_number)."""
        return invoice_number_key(self.invoice_number)


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
    ocr_pages: list[int] = Field(default_factory=list, description="1-based pages whose text came from OCR")
    error: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ocr_used(self) -> bool:
        return self.method in ("ocr", "mixed")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def method_label(self) -> str:
        """Plain description that keeps PDF text-layer extraction distinct from real OCR."""
        engine = self.ocr_engine or "OCR engine unavailable"
        pages = ", ".join(str(p) for p in self.ocr_pages)
        if self.method == "pdf_text":
            return "PDF text layer (embedded digital text, read directly; no OCR)"
        if self.method == "ocr":
            where = f"page(s) {pages}" if pages else "the image"
            return f"OCR ({engine}) on {where}: no text layer, text recognised from the image"
        if self.method == "mixed":
            return f"PDF text layer + OCR ({engine}) on scanned page(s) {pages}"
        return "No text extracted"

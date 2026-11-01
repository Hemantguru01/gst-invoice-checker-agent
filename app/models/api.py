"""Request/response schemas for the HTTP API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.invoice import ExtractionResult, InvoiceData
from app.models.validation import OverallStatus, ValidationSummary


class HealthResponse(BaseModel):
    status: str
    app: str
    version: str
    ocr_engine: str | None
    llm_configured: bool
    llm_model: str | None
    reference_data_version: str


class InvoiceRecord(BaseModel):
    invoice_id: str
    original_filename: str
    content_type: str
    size_bytes: int
    uploaded_at: datetime
    extraction: ExtractionResult | None = None
    overall_status: OverallStatus | None = None
    validated_at: datetime | None = None


class UploadResponse(BaseModel):
    invoice_id: str
    original_filename: str
    extraction: ExtractionResult


class ValidateRequest(BaseModel):
    invoice: InvoiceData | None = Field(
        default=None,
        description="Optional corrected invoice data. When given, it replaces the extracted data.",
    )
    use_llm: bool = Field(default=True, description="Use the LLM for explanations when configured")


class ValidateResponse(BaseModel):
    invoice_id: str
    overall_status: OverallStatus
    summary: ValidationSummary
    report_url: str


class HistoryItem(BaseModel):
    invoice_id: str
    original_filename: str
    uploaded_at: datetime
    extraction_status: str | None
    overall_status: OverallStatus | None
    validated_at: datetime | None


class ErrorResponse(BaseModel):
    error: str
    detail: str

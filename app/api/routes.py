"""HTTP endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Body, Depends, File, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

from app.api.dependencies import get_invoice_service
from app.core.config import get_settings
from app.core.exceptions import InvalidFileError
from app.models.api import (
    ErrorResponse,
    HealthResponse,
    HistoryItem,
    InvoiceRecord,
    UploadResponse,
    ValidateRequest,
    ValidateResponse,
)
from app.models.validation import OverallStatus, ValidationReport
from app.services.invoice_service import InvoiceService
from app.services.report_service import to_html, to_markdown

router = APIRouter()
ERRORS = {code: {"model": ErrorResponse} for code in (400, 404, 413, 415, 422)}


@router.get("/health", response_model=HealthResponse, tags=["system"])
def health(service: InvoiceService = Depends(get_invoice_service)) -> HealthResponse:
    settings = get_settings()
    engine = service.extractor.ocr_engine
    return HealthResponse(
        status="ok",
        app=settings.app_name,
        version=settings.app_version,
        ocr_engine=engine.name if engine else None,
        llm_configured=service.agent.llm is not None,
        llm_model=settings.llm_model if service.agent.llm is not None else None,
        reference_data_version=service.agent.hsn_reference.version,
    )


@router.post("/invoices/upload", response_model=UploadResponse, status_code=201, responses=ERRORS,
             tags=["invoices"], summary="Upload a PDF/PNG/JPG invoice and extract its fields")
async def upload_invoice(file: UploadFile = File(...),
                         service: InvoiceService = Depends(get_invoice_service)) -> UploadResponse:
    limit = service.settings.max_upload_bytes
    content = await file.read(limit + 1)  # read at most one byte over the limit
    invoice_id, safe_name, extraction = service.upload(file.filename, content)
    return UploadResponse(invoice_id=invoice_id, original_filename=safe_name, extraction=extraction)


@router.post("/invoices/{invoice_id}/validate", response_model=ValidateResponse, responses=ERRORS,
             tags=["invoices"], summary="Run the rule engine and agent; optionally with corrected data")
def validate_invoice(invoice_id: str, request: ValidateRequest | None = Body(default=None),
                     service: InvoiceService = Depends(get_invoice_service)) -> ValidateResponse:
    request = request or ValidateRequest()
    report = service.validate(invoice_id, request.invoice, request.use_llm)
    return ValidateResponse(invoice_id=invoice_id, overall_status=report.overall_status,
                            summary=report.summary, report_url=f"/invoices/{invoice_id}/report")


@router.get("/invoices", response_model=list[HistoryItem], tags=["invoices"], summary="Processing history")
def list_invoices(limit: int = Query(50, ge=1, le=500),
                  service: InvoiceService = Depends(get_invoice_service)) -> list[HistoryItem]:
    return [
        HistoryItem(invoice_id=r.id, original_filename=r.original_filename, uploaded_at=r.uploaded_at,
                    extraction_status=r.extraction_status,
                    overall_status=OverallStatus(r.overall_status) if r.overall_status else None,
                    validated_at=r.validated_at)
        for r in service.history(limit)
    ]


@router.get("/invoices/{invoice_id}", response_model=InvoiceRecord, responses=ERRORS, tags=["invoices"])
def get_invoice(invoice_id: str, service: InvoiceService = Depends(get_invoice_service)) -> InvoiceRecord:
    r = service.get(invoice_id)
    return InvoiceRecord(invoice_id=r.id, original_filename=r.original_filename, content_type=r.content_type,
                         size_bytes=r.size_bytes, uploaded_at=r.uploaded_at, extraction=r.extraction,
                         overall_status=OverallStatus(r.overall_status) if r.overall_status else None,
                         validated_at=r.validated_at)


@router.get("/invoices/{invoice_id}/report", response_model=ValidationReport, responses=ERRORS,
            tags=["invoices"], summary="Validation report as JSON, Markdown or HTML")
def get_report(invoice_id: str, format: Literal["json", "markdown", "html"] = "json",
               service: InvoiceService = Depends(get_invoice_service)):
    report = service.report(invoice_id)
    if format == "markdown":
        return PlainTextResponse(to_markdown(report), media_type="text/markdown; charset=utf-8")
    if format == "html":
        return HTMLResponse(to_html(report))
    return report


@router.get("/invoices/{invoice_id}/file", responses=ERRORS, tags=["invoices"],
            summary="Download the originally uploaded file")
def get_file(invoice_id: str, service: InvoiceService = Depends(get_invoice_service)) -> FileResponse:
    record = service.get(invoice_id)
    path = Path(record.stored_path).resolve()
    if path.parent != service.file_store.root or not path.is_file():
        raise InvalidFileError("Stored file is unavailable.")
    return FileResponse(path, media_type=record.content_type, filename=record.original_filename)

"""HTTP endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Body, Depends, File, Path as PathParam, Query, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

from app.api.dependencies import get_invoice_service
from app.core.config import get_settings
from app.core.exceptions import InvalidFileError
from app.core.security import REPORT_CSP, limit_expensive_requests, require_api_key
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
# Every invoice endpoint requires the API key when one is configured; /health stays open for probes.
invoices = APIRouter(dependencies=[Depends(require_api_key)])
ERRORS = {code: {"model": ErrorResponse} for code in (400, 401, 404, 413, 415, 422, 429, 503)}
# Only server-generated UUIDs are accepted as invoice IDs.
InvoiceId = PathParam(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


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


@invoices.post("/invoices/upload", response_model=UploadResponse, status_code=201, responses=ERRORS,
               tags=["invoices"], summary="Upload a PDF/PNG/JPG invoice and extract its fields",
               dependencies=[Depends(limit_expensive_requests)])
async def upload_invoice(file: UploadFile = File(...),
                         service: InvoiceService = Depends(get_invoice_service)) -> UploadResponse:
    limit = service.settings.max_upload_bytes
    content = await file.read(limit + 1)  # read at most one byte over the limit
    # OCR is CPU-bound: run it off the event loop so other requests are not blocked.
    invoice_id, safe_name, extraction = await run_in_threadpool(service.upload, file.filename, content)
    return UploadResponse(invoice_id=invoice_id, original_filename=safe_name, extraction=extraction)


@invoices.post("/invoices/{invoice_id}/validate", response_model=ValidateResponse, responses=ERRORS,
               tags=["invoices"], summary="Run the rule engine and agent; optionally with corrected data",
               dependencies=[Depends(limit_expensive_requests)])
def validate_invoice(invoice_id: str = InvoiceId, request: ValidateRequest | None = Body(default=None),
                     service: InvoiceService = Depends(get_invoice_service)) -> ValidateResponse:
    request = request or ValidateRequest()
    report = service.validate(invoice_id, request.invoice, request.use_llm)
    return ValidateResponse(invoice_id=invoice_id, overall_status=report.overall_status,
                            summary=report.summary, report_url=f"/invoices/{invoice_id}/report")


@invoices.get("/invoices", response_model=list[HistoryItem], tags=["invoices"], summary="Processing history")
def list_invoices(limit: int = Query(50, ge=1, le=500),
                  service: InvoiceService = Depends(get_invoice_service)) -> list[HistoryItem]:
    return [
        HistoryItem(invoice_id=r.id, original_filename=r.original_filename, uploaded_at=r.uploaded_at,
                    extraction_status=r.extraction_status,
                    overall_status=OverallStatus(r.overall_status) if r.overall_status else None,
                    validated_at=r.validated_at)
        for r in service.history(limit)
    ]


@invoices.get("/invoices/{invoice_id}", response_model=InvoiceRecord, responses=ERRORS, tags=["invoices"])
def get_invoice(invoice_id: str = InvoiceId, service: InvoiceService = Depends(get_invoice_service)) -> InvoiceRecord:
    r = service.get(invoice_id)
    return InvoiceRecord(invoice_id=r.id, original_filename=r.original_filename, content_type=r.content_type,
                         size_bytes=r.size_bytes, uploaded_at=r.uploaded_at, extraction=r.extraction,
                         overall_status=OverallStatus(r.overall_status) if r.overall_status else None,
                         validated_at=r.validated_at)


@invoices.get("/invoices/{invoice_id}/report", response_model=ValidationReport, responses=ERRORS,
              tags=["invoices"], summary="Validation report as JSON, Markdown or HTML")
def get_report(invoice_id: str = InvoiceId, format: Literal["json", "markdown", "html"] = "json",
               service: InvoiceService = Depends(get_invoice_service)):
    report = service.report(invoice_id)
    if format == "markdown":
        return PlainTextResponse(to_markdown(report), media_type="text/markdown; charset=utf-8")
    if format == "html":
        return HTMLResponse(to_html(report), headers={"Content-Security-Policy": REPORT_CSP})
    return report


@invoices.get("/invoices/{invoice_id}/file", responses=ERRORS, tags=["invoices"],
              summary="Download the originally uploaded file")
def get_file(invoice_id: str = InvoiceId, service: InvoiceService = Depends(get_invoice_service)) -> FileResponse:
    record = service.get(invoice_id)
    path = Path(record.stored_path).resolve()
    if path.parent != service.file_store.root or not path.is_file():
        raise InvalidFileError("Stored file is unavailable.")
    return FileResponse(path, media_type=record.content_type, filename=record.original_filename)


@invoices.delete("/invoices/{invoice_id}", status_code=204, responses=ERRORS, tags=["invoices"],
                 summary="Delete the uploaded file, extracted data and reports for an invoice")
def delete_invoice(invoice_id: str = InvoiceId,
                   service: InvoiceService = Depends(get_invoice_service)) -> Response:
    service.delete(invoice_id)
    return Response(status_code=204)


router.include_router(invoices)

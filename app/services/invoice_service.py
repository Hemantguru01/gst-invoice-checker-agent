"""Application service tying upload, extraction, validation and persistence together."""

from __future__ import annotations

import logging
import uuid

from app.agent.invoice_agent import InvoiceCheckerAgent
from app.core.config import Settings
from app.core.exceptions import InvoiceNotFoundError, ReportNotFoundError
from app.models.invoice import ExtractionResult, ExtractionStatus, FieldSource, InvoiceData
from app.models.validation import ValidationReport
from app.services.extraction_service import ExtractionService, missing_fields
from app.services.file_service import FileStore, validate_upload
from app.services.report_service import save_report_files
from app.services.storage import InvoiceRepository, StoredInvoice
from app.validators.fields import MANDATORY

logger = logging.getLogger(__name__)


class InvoiceService:
    def __init__(self, settings: Settings, repository: InvoiceRepository, file_store: FileStore,
                 extractor: ExtractionService, agent: InvoiceCheckerAgent) -> None:
        self.settings = settings
        self.repository = repository
        self.file_store = file_store
        self.extractor = extractor
        self.agent = agent

    def upload(self, filename: str | None, content: bytes) -> tuple[str, str, ExtractionResult]:
        validated = validate_upload(filename, content, self.settings.max_upload_bytes,
                                    self.settings.max_pdf_pages)
        invoice_id = str(uuid.uuid4())
        path = self.file_store.save(invoice_id, validated.kind, content)
        extraction = self.extractor.extract(content, validated.kind)
        self.repository.create(invoice_id, validated.safe_name, validated.content_type, validated.kind,
                               validated.size, path, extraction)
        logger.info("Invoice %s uploaded (%s, %d bytes); extraction=%s",
                    invoice_id, validated.kind, validated.size, extraction.status.value)
        return invoice_id, validated.safe_name, extraction

    def get(self, invoice_id: str) -> StoredInvoice:
        record = self.repository.get(invoice_id)
        if record is None:
            raise InvoiceNotFoundError(f"Invoice '{invoice_id}' was not found.")
        return record

    def validate(self, invoice_id: str, corrected: InvoiceData | None = None,
                 use_llm: bool = True) -> ValidationReport:
        record = self.get(invoice_id)
        extraction = record.extraction or ExtractionResult(status=ExtractionStatus.FAILED, method="none")
        if corrected is not None:
            extraction = self._apply_corrections(extraction, corrected)
        report = self.agent.run(invoice_id, extraction, original_filename=record.original_filename,
                                use_llm=use_llm)
        self.repository.save_report(invoice_id, report)
        save_report_files(report, self.settings.reports_dir)
        logger.info("Invoice %s validated: %s", invoice_id, report.overall_status.value)
        return report

    def report(self, invoice_id: str) -> ValidationReport:
        record = self.get(invoice_id)
        if record.report is None:
            raise ReportNotFoundError(
                f"Invoice '{invoice_id}' has not been validated yet. Call POST /invoices/{invoice_id}/validate."
            )
        return record.report

    def history(self, limit: int = 50) -> list[StoredInvoice]:
        return self.repository.list(limit)

    def _apply_corrections(self, extraction: ExtractionResult, corrected: InvoiceData) -> ExtractionResult:
        from app.services.reference_data import get_state_codes

        if corrected.place_of_supply and not corrected.place_of_supply_code:
            corrected.place_of_supply_code = get_state_codes().code_for_name(corrected.place_of_supply)
        sources = dict(extraction.field_sources)
        original = extraction.invoice.model_dump()
        for name, value in corrected.model_dump().items():
            if value != original.get(name) and value not in (None, []):
                sources[name] = FieldSource.USER
        missing = missing_fields(corrected)
        status = (ExtractionStatus.PARTIAL if any(f in missing for f in MANDATORY)
                  else ExtractionStatus.SUCCESS)
        return extraction.model_copy(update={
            "invoice": corrected,
            "status": status,
            "missing_fields": missing,
            "field_sources": sources,
            "uncertain_fields": [f for f in extraction.uncertain_fields if sources.get(f) != FieldSource.USER],
            "error": None if status is not ExtractionStatus.FAILED else extraction.error,
        })

"""Application service tying upload, extraction, validation and persistence together."""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone

from app.agent.invoice_agent import InvoiceCheckerAgent
from app.core.config import Settings
from app.core.exceptions import InvoiceNotFoundError, ReportNotFoundError, ServiceBusyError
from app.models.invoice import ExtractionResult, ExtractionStatus, FieldSource, InvoiceData
from app.models.validation import ValidationReport
from app.services.extraction_service import ExtractionService, missing_fields
from app.services.file_service import FileStore, validate_upload
from app.services.report_service import delete_report_files, save_report_files
from app.services.storage import DuplicateCandidate, InvoiceRepository, StoredInvoice, financial_year
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
        # OCR is CPU- and memory-heavy: cap how many run at once so a burst of uploads cannot exhaust the host.
        self._extraction_slots = threading.BoundedSemaphore(settings.max_concurrent_extractions)

    def upload(self, filename: str | None, content: bytes) -> tuple[str, str, ExtractionResult]:
        validated = validate_upload(filename, content, self.settings.max_upload_bytes,
                                    self.settings.max_pdf_pages)
        self.purge_expired()  # retention is enforced continuously, not only at startup
        if not self._extraction_slots.acquire(timeout=self.settings.extraction_wait_seconds):
            raise ServiceBusyError("The server is busy processing other invoices. Try again shortly.")
        try:
            extraction = self.extractor.extract(content, validated.kind)
        finally:
            self._extraction_slots.release()
        invoice_id = str(uuid.uuid4())
        path = self.file_store.save(invoice_id, validated.kind, content)
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
                                use_llm=use_llm, duplicates=self._duplicates(invoice_id, extraction))
        self.repository.save_report(invoice_id, report)
        save_report_files(report, self.settings.reports_dir)
        logger.info("Invoice %s validated: %s", invoice_id, report.overall_status.value)
        return report

    def _duplicates(self, invoice_id: str, extraction: ExtractionResult) -> list[DuplicateCandidate] | None:
        inv = extraction.invoice
        if not inv.supplier_gstin or not inv.invoice_number_key:
            return None  # nothing reliable to compare
        return self.repository.find_same_invoice_number(invoice_id, inv.supplier_gstin, inv.invoice_number_key,
                                                        financial_year(inv.invoice_date))

    def report(self, invoice_id: str) -> ValidationReport:
        record = self.get(invoice_id)
        if record.report is None:
            raise ReportNotFoundError(
                f"Invoice '{invoice_id}' has not been validated yet. Call POST /invoices/{invoice_id}/validate."
            )
        return record.report

    def history(self, limit: int = 50) -> list[StoredInvoice]:
        return self.repository.list(limit)

    def delete(self, invoice_id: str) -> None:
        """Remove the uploaded file, saved report files and the database record."""
        record = self.get(invoice_id)
        self._remove(record.id, record.stored_path)
        logger.info("Invoice %s deleted on request.", invoice_id)

    def purge_expired(self, now: datetime | None = None) -> int:
        if self.settings.retention_days <= 0:
            return 0
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=self.settings.retention_days)
        expired = self.repository.ids_uploaded_before(cutoff)
        for invoice_id, stored_path in expired:
            self._remove(invoice_id, stored_path)
        return len(expired)

    def _remove(self, invoice_id: str, stored_path: str) -> None:
        self.file_store.delete(stored_path)
        delete_report_files(invoice_id, self.settings.reports_dir)
        self.repository.delete(invoice_id)

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

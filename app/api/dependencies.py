"""Builds the service graph once per process."""

from __future__ import annotations

from functools import lru_cache

from app.agent.invoice_agent import InvoiceCheckerAgent
from app.agent.llm_client import build_llm_client
from app.core.config import get_settings
from app.services.extraction_service import ExtractionService
from app.services.file_service import FileStore
from app.services.invoice_service import InvoiceService
from app.services.ocr_service import get_ocr_engine
from app.services.reference_data import get_hsn_reference, get_state_codes
from app.services.storage import InvoiceRepository
from app.validators.engine import RuleEngine


@lru_cache
def get_invoice_service() -> InvoiceService:
    settings = get_settings()
    settings.ensure_directories()
    llm = build_llm_client(settings)
    hsn = get_hsn_reference()
    states = get_state_codes()
    extractor = ExtractionService(
        get_ocr_engine(settings.ocr_engine, settings.tesseract_cmd), llm, states,
        dpi=settings.ocr_render_dpi, max_pages=settings.max_pdf_pages,
        llm_assisted=settings.llm_assisted_extraction,
    )
    agent = InvoiceCheckerAgent(RuleEngine(hsn, states, settings.rounding_tolerance), hsn, llm)
    return InvoiceService(settings, InvoiceRepository(settings.database_path), FileStore(settings.upload_dir),
                          extractor, agent)

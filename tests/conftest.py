"""Shared fixtures. All invoice data here is synthetic."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.agent.invoice_agent import InvoiceCheckerAgent
from app.core.config import Settings
from app.models.invoice import ExtractionResult, ExtractionStatus, InvoiceData, LineItem
from app.services.extraction_service import ExtractionService, missing_fields
from app.services.file_service import FileStore
from app.services.invoice_service import InvoiceService
from app.services.reference_data import get_hsn_reference, get_state_codes
from app.services.storage import InvoiceRepository
from app.utils.sample_invoice import RECIPIENT_GJ, RECIPIENT_KA, SUPPLIER_KA, SUPPLIER_MH
from app.validators.engine import RuleEngine

TODAY = date(2026, 11, 2)
D = Decimal


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    from app.core.security import rate_limiter

    rate_limiter.reset()
    yield
    rate_limiter.reset()


@pytest.fixture(scope="session")
def hsn_ref():
    return get_hsn_reference()


@pytest.fixture(scope="session")
def state_codes():
    return get_state_codes()


@pytest.fixture
def engine(hsn_ref, state_codes) -> RuleEngine:
    return RuleEngine(hsn_ref, state_codes, rounding_tolerance=1.0)


def intra_state_invoice(**overrides: Any) -> InvoiceData:
    data = InvoiceData(
        supplier_name="Sahyadri Tech Solutions Pvt Ltd",
        supplier_gstin=SUPPLIER_KA,
        recipient_name="Kaveri Retail Traders",
        recipient_gstin=RECIPIENT_KA,
        invoice_number="STS/25-26/0142",
        invoice_date=date(2025, 10, 15),
        place_of_supply="29-Karnataka",
        place_of_supply_code="29",
        line_items=[
            LineItem(description="Laptop", hsn_sac="8471", quantity=D("2"), unit_price=D("45000"),
                     taxable_value=D("90000"), gst_rate=D("18")),
            LineItem(description="Software development", hsn_sac="998314", quantity=D("40"),
                     unit_price=D("1500"), taxable_value=D("60000"), gst_rate=D("18")),
        ],
        taxable_value=D("150000"),
        cgst=D("13500"),
        sgst=D("13500"),
        total_invoice_value=D("177000"),
    )
    return data.model_copy(update=overrides)


def inter_state_invoice(**overrides: Any) -> InvoiceData:
    data = InvoiceData(
        supplier_name="Mahalaxmi Office Systems LLP",
        supplier_gstin=SUPPLIER_MH,
        recipient_name="Vadodara Print Hub",
        recipient_gstin=RECIPIENT_GJ,
        invoice_number="MOS/2025/1187",
        invoice_date=date(2025, 11, 3),
        place_of_supply="24-Gujarat",
        place_of_supply_code="24",
        line_items=[
            LineItem(description="Laser printer", hsn_sac="8443", quantity=D("3"), unit_price=D("12500"),
                     taxable_value=D("37500"), gst_rate=D("18")),
        ],
        taxable_value=D("37500"),
        igst=D("6750"),
        total_invoice_value=D("44250"),
    )
    return data.model_copy(update=overrides)


def as_extraction(invoice: InvoiceData, **kwargs: Any) -> ExtractionResult:
    return ExtractionResult(status=kwargs.pop("status", ExtractionStatus.SUCCESS), method="pdf_text",
                            invoice=invoice, missing_fields=missing_fields(invoice), **kwargs)


def checks_by_id(result) -> dict[str, Any]:
    return {c.check_id: c for c in result.checks}


@pytest.fixture
def make_service(tmp_path: Path, hsn_ref, state_codes):
    """Builds an isolated InvoiceService (temp DB/uploads/reports, no real LLM by default)."""

    def _make(ocr_engine=None, llm=None, **overrides: Any) -> InvoiceService:
        settings = Settings(
            _env_file=None,
            data_dir=tmp_path,
            upload_dir=tmp_path / "uploads",
            reports_dir=tmp_path / "reports",
            database_path=tmp_path / "test.db",
            anthropic_api_key=None,
            **overrides,
        )
        settings.ensure_directories()
        extractor = ExtractionService(ocr_engine, llm, state_codes)
        agent = InvoiceCheckerAgent(RuleEngine(hsn_ref, state_codes), hsn_ref, llm)
        return InvoiceService(settings, InvoiceRepository(settings.database_path),
                              FileStore(settings.upload_dir), extractor, agent)

    return _make

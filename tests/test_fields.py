from datetime import date

from app.models.invoice import ExtractionStatus, InvoiceData
from app.models.validation import CheckStatus, OverallStatus
from tests.conftest import TODAY, as_extraction, checks_by_id, intra_state_invoice


def test_missing_mandatory_fields_are_reported(engine):
    invoice = intra_state_invoice(invoice_number=None, invoice_date=None, total_invoice_value=None)
    result = engine.run(as_extraction(invoice), today=TODAY)
    checks = checks_by_id(result)
    for field in ("invoice_number", "invoice_date", "total_invoice_value"):
        assert checks[f"fields.{field}"].status is CheckStatus.MISSING
        assert checks[f"fields.{field}"].suggestion
    assert result.overall_status is OverallStatus.INVALID


def test_missing_recipient_gstin_needs_review_not_error(engine):
    result = engine.run(as_extraction(intra_state_invoice(recipient_gstin=None)), today=TODAY)
    assert checks_by_id(result)["fields.recipient_gstin"].status is CheckStatus.MANUAL_REVIEW
    assert result.overall_status is OverallStatus.NEEDS_REVIEW


def test_unparsed_date_goes_to_manual_review(engine):
    invoice = intra_state_invoice(invoice_date=None, invoice_date_raw="31-02-2025")
    check = checks_by_id(engine.run(as_extraction(invoice), today=TODAY))["fields.invoice_date"]
    assert check.status is CheckStatus.MANUAL_REVIEW


def test_future_invoice_date_fails(engine):
    invoice = intra_state_invoice(invoice_date=date(2027, 1, 1))
    check = checks_by_id(engine.run(as_extraction(invoice), today=TODAY))["fields.invoice_date.future"]
    assert check.status is CheckStatus.FAIL


def test_long_invoice_number_warns(engine):
    invoice = intra_state_invoice(invoice_number="INV/2025-26/000000012345")
    check = checks_by_id(engine.run(as_extraction(invoice), today=TODAY))["fields.invoice_number.format"]
    assert check.status is CheckStatus.WARNING


def test_uncertain_fields_need_review(engine):
    extraction = as_extraction(intra_state_invoice(), uncertain_fields=["supplier_gstin"])
    result = engine.run(extraction, today=TODAY)
    assert checks_by_id(result)["fields.supplier_gstin.uncertain"].status is CheckStatus.MANUAL_REVIEW
    assert result.overall_status is OverallStatus.NEEDS_REVIEW


def test_failed_extraction_is_invalid(engine):
    extraction = as_extraction(InvoiceData(), status=ExtractionStatus.FAILED, error="OCR failed")
    result = engine.run(extraction, today=TODAY)
    assert checks_by_id(result)["extraction.status"].status is CheckStatus.FAIL
    assert result.overall_status is OverallStatus.INVALID

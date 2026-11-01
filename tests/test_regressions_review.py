"""Regression tests: manual-review reporting, GSTIN registration status, HSN metadata, place of supply."""

import json
from decimal import Decimal
from pathlib import Path

from app.agent.invoice_agent import STANDING_MANUAL_ITEMS, InvoiceCheckerAgent
from app.core.config import PROJECT_ROOT
from app.models.invoice import ExtractionResult, ExtractionStatus, FieldSource
from app.models.validation import CheckStatus, OverallStatus
from app.services.extraction_service import ExtractionService
from app.services.invoice_parser import InvoiceParser
from app.services.ocr_service import TextLine
from app.utils.sample_invoice import (
    SUPPLIER_KA,
    export_invoice,
    ocr_confusable_gstin,
    render_pdf,
)
from app.validators.gstin_rules import ocr_alternatives
from tests.conftest import TODAY, as_extraction, checks_by_id, intra_state_invoice

D = Decimal


def _report(engine, hsn_ref, invoice=None, extraction=None):
    extraction = extraction or as_extraction(invoice or intra_state_invoice())
    return InvoiceCheckerAgent(engine, hsn_ref, None).run("id", extraction, today=TODAY)


# --- 1. Manual-review count -------------------------------------------------------------------

def test_valid_invoice_never_reports_zero_human_verification(engine, hsn_ref):
    report = _report(engine, hsn_ref)
    s = report.summary
    assert report.overall_status is OverallStatus.VALID
    assert s.manual_review == 0  # no automated finding needs review...
    assert s.standing_reminders == len(report.standing_reminders) >= 5  # ...but people still must check
    assert s.manual_verification_total == s.manual_review + s.standing_reminders
    assert report.manual_verification == report.manual_review_findings + report.standing_reminders
    assert "standing verification reminders" in report.overall_explanation


def test_automated_findings_and_reminders_are_counted_separately(engine, hsn_ref):
    report = _report(engine, hsn_ref, intra_state_invoice(place_of_supply=None, place_of_supply_code=None))
    s = report.summary
    assert s.manual_review == len(report.manual_review_findings) >= 1
    assert any(f.startswith("Supply type") for f in report.manual_review_findings)
    assert not any(f.startswith("Supply type") for f in report.standing_reminders)
    assert report.overall_status is OverallStatus.NEEDS_REVIEW


def test_ocr_extraction_adds_an_ocr_reminder(engine, hsn_ref):
    extraction = as_extraction(intra_state_invoice()).model_copy(
        update={"method": "ocr", "ocr_engine": "rapidocr", "ocr_pages": [1]})
    report = _report(engine, hsn_ref, extraction=extraction)
    assert any("read by OCR" in r for r in report.standing_reminders)


# --- 3. GSTIN status ----------------------------------------------------------------------------

def test_checksum_pass_is_not_reported_as_registration(engine, hsn_ref):
    report = _report(engine, hsn_ref)
    checksum = checks_by_id(report)["gstin.supplier.checksum"]
    assert checksum.status is CheckStatus.PASS
    assert "does not show that it is registered or active" in checksum.message
    assert set(report.gstin_registration) == {"supplier", "recipient"}
    assert all("not verified" in v for v in report.gstin_registration.values())
    supplier_reminder = next(r for r in report.standing_reminders if r.startswith("Supplier GSTIN"))
    assert "NOT verified" in supplier_reminder


def test_report_never_claims_a_gstin_is_active(engine, hsn_ref):
    text = _report(engine, hsn_ref).model_dump_json().lower()
    for claim in ("gstin is active", "is registered and active", "registration: active", "status: active"):
        assert claim not in text


def test_ocr_confusable_gstin_goes_to_manual_review_not_fail(engine, state_codes):
    misread = ocr_confusable_gstin()
    assert ocr_alternatives(misread, state_codes.codes) == [SUPPLIER_KA]
    invoice = intra_state_invoice(supplier_gstin=misread)
    from_ocr = as_extraction(invoice, field_sources={"supplier_gstin": FieldSource.OCR})
    checks = checks_by_id(engine.run(from_ocr, today=TODAY))
    check = checks["gstin.supplier.ocr_misread"]
    assert check.status is CheckStatus.MANUAL_REVIEW
    assert SUPPLIER_KA in check.message and "not changed" in check.message
    assert "gstin.supplier.format" not in checks


def test_same_gstin_from_pdf_text_is_still_a_format_error(engine):
    invoice = intra_state_invoice(supplier_gstin=ocr_confusable_gstin())
    pdf = as_extraction(invoice, field_sources={"supplier_gstin": FieldSource.PDF_TEXT})
    checks = checks_by_id(engine.run(pdf, today=TODAY))
    assert checks["gstin.supplier.format"].status is CheckStatus.FAIL


# --- 4. HSN/SAC reference data ------------------------------------------------------------------

def test_every_reference_entry_has_effective_date_and_source():
    raw = json.loads((PROJECT_ROOT / "data/reference/hsn_sac_rates.json").read_text(encoding="utf-8"))
    assert "NOT LEGALLY AUTHORITATIVE" in raw["metadata"]["legal_status"]
    for entry in raw["entries"]:
        assert entry["effective_from"] and entry["source"]
        assert entry["notification_ref"] is None  # nothing is claimed as verified against a notification


def test_rate_pass_is_labelled_non_authoritative(engine):
    check = checks_by_id(engine.run(as_extraction(intra_state_invoice()), today=TODAY))["hsn.line1.rate"]
    assert check.status is CheckStatus.PASS
    assert "not legally authoritative" in check.message
    assert "notification: not linked" in check.expected


def test_unknown_code_makes_the_invoice_need_review(engine):
    items = intra_state_invoice().line_items
    items[0] = items[0].model_copy(update={"hsn_sac": "7323"})
    result = engine.run(as_extraction(intra_state_invoice(line_items=items)), today=TODAY)
    assert checks_by_id(result)["hsn.line1.code"].status is CheckStatus.MANUAL_REVIEW
    assert result.overall_status is OverallStatus.NEEDS_REVIEW


def test_low_confidence_rate_never_passes(engine):
    items = intra_state_invoice().line_items
    items[1] = items[1].model_copy(update={"hsn_sac": "3004", "gst_rate": D("5"), "taxable_value": D("60000")})
    invoice = intra_state_invoice(line_items=items, cgst=D("9600"), sgst=D("9600"), total_invoice_value=D("169200"))
    result = engine.run(as_extraction(invoice), today=TODAY)
    assert checks_by_id(result)["hsn.line2.rate"].status is CheckStatus.MANUAL_REVIEW
    assert result.overall_status is OverallStatus.NEEDS_REVIEW


# --- 5. Place of supply -------------------------------------------------------------------------

def test_export_under_lut_is_not_auto_classified(engine):
    extraction = ExtractionService(None, None).extract(render_pdf(export_invoice()), "pdf")
    assert extraction.invoice.place_of_supply_code == "96"
    assert extraction.invoice.supply_indicators
    result = engine.run(extraction, today=TODAY)
    checks = checks_by_id(result)
    assert checks["supply.type"].status is CheckStatus.MANUAL_REVIEW
    assert "calc.tax_type" not in checks and "calc.igst" not in checks
    assert checks["hsn.line1.rate"].status is CheckStatus.MANUAL_REVIEW  # 0% vs domestic 18%
    assert checks["calc.invoice_total_expected"].status is CheckStatus.MANUAL_REVIEW
    assert result.overall_status is OverallStatus.NEEDS_REVIEW  # not INVALID, not VALID


def test_sez_wording_is_detected(state_codes):
    lines = [TextLine("Supplier: Sahyadri Tech Solutions Pvt Ltd"), TextLine(f"GSTIN: {SUPPLIER_KA}"),
             TextLine("Bill To: Unit 4, Special Economic Zone, Electronic City"),
             TextLine("Place of Supply: 29-Karnataka"),
             TextLine("Supply to SEZ unit under LUT without payment of integrated tax")]
    invoice = InvoiceParser(state_codes).parse(lines).invoice
    assert {"SEZ", "LUT"} <= set(invoice.supply_indicators)


def test_sez_supply_needs_review(engine):
    invoice = intra_state_invoice(supply_indicators=["SEZ"])
    result = engine.run(as_extraction(invoice), today=TODAY)
    assert checks_by_id(result)["supply.type"].status is CheckStatus.MANUAL_REVIEW
    assert result.overall_status is not OverallStatus.VALID


def test_recipient_state_different_from_place_of_supply_needs_review(engine):
    from app.utils.sample_invoice import RECIPIENT_GJ

    result = engine.run(as_extraction(intra_state_invoice(recipient_gstin=RECIPIENT_GJ)), today=TODAY)
    check = checks_by_id(result)["supply.pos_rule"]
    assert check.status is CheckStatus.MANUAL_REVIEW
    assert "special place-of-supply rules" in check.explanation  # services lines present
    assert result.overall_status is OverallStatus.NEEDS_REVIEW


def test_standing_items_do_not_duplicate_gstin_reminder():
    assert not any("GSTIN" in item for item in STANDING_MANUAL_ITEMS)

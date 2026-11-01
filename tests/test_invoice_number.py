"""Invoice number extraction, normalisation, Rule 46 format check and duplicate detection."""

import sqlite3
from dataclasses import replace

import pytest

from app.models.invoice import InvoiceData
from app.models.validation import CheckStatus, OverallStatus
from app.services.extraction_service import ExtractionService
from app.services.invoice_parser import InvoiceParser
from app.services.ocr_service import TextLine, get_ocr_engine, restore_word_spaces
from app.services.storage import InvoiceRepository, financial_year
from app.utils.invoice_number import invoice_number_key, normalize_invoice_number, rule46_problem
from app.utils.sample_invoice import SUPPLIER_MH, pdf_to_png, render_pdf, valid_intra_state
from tests.conftest import TODAY, as_extraction, checks_by_id, intra_state_invoice

FORMATS = [
    # printed,             display,          comparison key
    ("INV/2026/001",       "INV/2026/001",   "INV/2026/001"),
    ("INV / 2026 / 001",   "INV/2026/001",   "INV/2026/001"),
    ("INV-2026-001",       "INV-2026-001",   "INV-2026-001"),
    ("GST 001",            "GST 001",        "GST001"),
    ("GST001",             "GST001",         "GST001"),
]


@pytest.mark.parametrize("printed,display,key", FORMATS)
def test_normalisation(printed, display, key):
    assert normalize_invoice_number(printed) == display
    assert invoice_number_key(printed) == key
    assert rule46_problem(printed) is None  # all five are accepted


@pytest.mark.parametrize("printed,display,key", FORMATS)
def test_parser_extracts_the_whole_number(state_codes, printed, display, key):
    parser = InvoiceParser(state_codes)
    for line in (f"Invoice No: {printed}  Invoice Date: 15-10-2025",  # column gap
                 f"Invoice No: {printed} Invoice Date: 15-10-2025",   # single space before the next label
                 f"Invoice No: {printed}"):
        inv = parser.parse([TextLine(line)]).invoice
        assert inv.invoice_number == display, line
        assert inv.invoice_number_raw == printed, line  # original kept for audit
        assert inv.invoice_number_key == key


@pytest.mark.parametrize("printed,display,key", FORMATS)
def test_validation_passes_without_manual_editing(engine, printed, display, key):
    extraction = ExtractionService(None, None).extract(
        render_pdf(replace(valid_intra_state(), invoice_number=printed)), "pdf")
    assert extraction.invoice.invoice_number_raw == printed
    result = engine.run(extraction, today=TODAY)
    checks = checks_by_id(result)
    assert "fields.invoice_number.format" not in checks
    assert result.overall_status is OverallStatus.VALID


def test_separators_are_never_merged():
    keys = {invoice_number_key(v) for v in ("INV/2026/001", "INV-2026-001", "INV2026001", "INV/2026/01")}
    assert len(keys) == 4
    assert invoice_number_key("inv/2026/001") == invoice_number_key("INV/2026/001")
    assert invoice_number_key("INV–2026–001") == "INV-2026-001"  # en dash from a PDF


def test_whitespace_is_the_only_thing_normalised():
    assert normalize_invoice_number("  inv  /  2026 /001 ") == "inv/2026/001"  # case kept for display
    assert normalize_invoice_number("AB  12") == "AB 12"
    assert normalize_invoice_number("O0I1-S5") == "O0I1-S5"  # look-alike characters are never "fixed"


def test_rule46_limits_still_apply():
    assert "17 characters" in rule46_problem("INV/2026/00000001")
    assert "characters other than" in rule46_problem("INV#2026")
    assert rule46_problem("ABCDEFGH 12345678") is None  # exactly 16 once the space is ignored
    assert rule46_problem("ABCDEFGH 123456789") is not None  # 17


def test_corrected_number_becomes_its_own_original():
    inv = InvoiceData(invoice_number="INV / 2026 / 001")
    assert (inv.invoice_number, inv.invoice_number_raw) == ("INV/2026/001", "INV / 2026 / 001")
    corrected = InvoiceData.model_validate({**inv.model_dump(), "invoice_number": "INV/2026/002"})
    assert corrected.invoice_number_raw == "INV/2026/002"
    reloaded = InvoiceData.model_validate(inv.model_dump())  # round trip keeps the printed form
    assert reloaded.invoice_number_raw == "INV / 2026 / 001"


# --- OCR must not alter invoice numbers ----------------------------------------------------------

def test_word_space_restoration_leaves_identifiers_alone():
    assert restore_word_spaces("InvoiceNo: eInv/26/001 BillTo:") == "Invoice No: eInv/26/001 Bill To:"
    assert restore_word_spaces("GSTIN: 29AAQCS4821K1Z5") == "GSTIN: 29AAQCS4821K1Z5"


def test_parser_reads_invoice_number_from_unmodified_ocr_text(state_codes):
    line = TextLine("Invoice No: e Inv/26/001", 0.97, raw_text="InvoiceNo: eInv/26/001")
    inv = InvoiceParser(state_codes).parse([line]).invoice
    assert inv.invoice_number == "eInv/26/001"


def test_normalisation_is_reported_not_silent(state_codes):
    result = InvoiceParser(state_codes).parse([TextLine("Invoice No: INV / 2026 / 001")])
    assert any("printed as 'INV / 2026 / 001'" in w for w in result.warnings)


@pytest.mark.parametrize("printed", ["INV / 2026 / 001", "GST 001"])
def test_ocr_keeps_the_invoice_number(printed):
    engine = get_ocr_engine("auto")
    if engine is None:
        pytest.skip("No OCR engine installed")
    png = pdf_to_png(render_pdf(replace(valid_intra_state(), invoice_number=printed)))
    inv = ExtractionService(engine, None).extract(png, "png").invoice
    assert inv.invoice_number_key == invoice_number_key(printed)  # no character added, dropped or changed


# --- duplicate detection --------------------------------------------------------------------------

def _validate(service, printed, supplier=None, date_str=None):
    sample = replace(valid_intra_state(), invoice_number=printed)
    if supplier:
        sample = replace(sample, supplier_gstin=supplier)
    if date_str:
        sample = replace(sample, invoice_date=date_str)
    invoice_id, _, _ = service.upload(f"{printed}.pdf", render_pdf(sample))
    return checks_by_id(service.validate(invoice_id))["fields.invoice_number.duplicate"]


def test_duplicate_detection_uses_the_comparison_key(make_service):
    service = make_service()
    assert _validate(service, "INV/2026/001").status is CheckStatus.PASS
    same = _validate(service, "INV / 2026 / 001")
    assert same.status is CheckStatus.WARNING
    assert "printed as 'INV/2026/001'" in same.message and "not merged" in same.explanation
    assert _validate(service, "INV-2026-001").status is CheckStatus.PASS  # different separator, different invoice
    assert _validate(service, "INV/2026/001", supplier=SUPPLIER_MH).status is CheckStatus.PASS  # other supplier
    assert _validate(service, "INV/2026/001", date_str="15-10-2026").status is CheckStatus.PASS  # FY 2026-27
    assert len(service.history()) == 5  # nothing merged or dropped


def test_spacing_only_difference_is_flagged_for_review(make_service):
    service = make_service()
    _validate(service, "GST001")
    check = _validate(service, "GST 001")
    assert check.status is CheckStatus.WARNING
    assert "'GST001'" in check.message


def test_identical_reupload_is_flagged(make_service):
    service = make_service()
    _validate(service, "INV/2026/001")
    check = _validate(service, "INV/2026/001")
    assert check.status is CheckStatus.WARNING and "(identical)" in check.message


def test_engine_without_history_reports_no_duplicate_check(engine):
    assert "fields.invoice_number.duplicate" not in checks_by_id(
        engine.run(as_extraction(intra_state_invoice()), today=TODAY))


def test_financial_year():
    from datetime import date

    assert financial_year(date(2026, 3, 31)) == "2025-26"
    assert financial_year(date(2026, 4, 1)) == "2026-27"
    assert financial_year(None) is None


def test_existing_database_is_migrated(tmp_path):
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE invoices (id TEXT PRIMARY KEY, original_filename TEXT NOT NULL, "
                     "content_type TEXT NOT NULL, kind TEXT NOT NULL, size_bytes INTEGER NOT NULL, "
                     "stored_path TEXT NOT NULL, uploaded_at TEXT NOT NULL, extraction_status TEXT, "
                     "extraction_json TEXT, overall_status TEXT, report_json TEXT, validated_at TEXT)")
    InvoiceRepository(db)
    with sqlite3.connect(db) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(invoices)")}
    assert {"supplier_gstin", "invoice_number_raw", "invoice_number_key", "financial_year"} <= columns

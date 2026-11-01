from datetime import date
from decimal import Decimal

import pytest

from app.agent.llm_extraction import fill_missing_fields, is_grounded
from app.models.invoice import ExtractionStatus, FieldSource, InvoiceData
from app.services.extraction_service import ExtractionService
from app.services.invoice_parser import InvoiceParser, parse_date
from app.services.ocr_service import TextLine, TextSegment, get_ocr_engine, restore_word_spaces
from app.utils.money import fmt_inr, parse_amount
from app.utils.sample_invoice import (
    SUPPLIER_KA,
    invalid_invoice,
    missing_fields_invoice,
    pdf_to_png,
    png_to_scanned_pdf,
    render_pdf,
    valid_inter_state,
    valid_intra_state,
)

D = Decimal


@pytest.fixture(scope="module")
def ocr_engine():
    engine = get_ocr_engine("auto")
    if engine is None:
        pytest.skip("No OCR engine installed")
    return engine


class BrokenOCR:
    name = "broken"

    def recognize(self, image):
        raise RuntimeError("model crashed")


class EmptyOCR:
    name = "empty"

    def recognize(self, image):
        return []


@pytest.mark.parametrize("raw,expected", [
    ("1,06,200.00", D("106200.00")), ("₹ 500", D("500")), ("Rs. 1,200/-", D("1200")),
    ("abc", None), ("", None), (None, None),
])
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == expected


def test_fmt_inr_uses_indian_grouping():
    assert fmt_inr(D("177000")) == "₹1,77,000.00"
    assert fmt_inr(D("12345678.5")) == "₹1,23,45,678.50"
    assert fmt_inr(D("999")) == "₹999.00"


@pytest.mark.parametrize("raw,expected", [
    ("15-10-2025", date(2025, 10, 15)), ("15/10/2025", date(2025, 10, 15)),
    ("2025-10-15", date(2025, 10, 15)), ("15 Oct 2025", date(2025, 10, 15)),
    ("31-02-2025", None), ("someday", None),
])
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


def test_text_pdf_extraction_intra_state(state_codes):
    result = ExtractionService(None, None, state_codes).extract(render_pdf(valid_intra_state()), "pdf")
    inv = result.invoice
    assert result.status is ExtractionStatus.SUCCESS
    assert result.method == "pdf_text"
    assert inv.supplier_gstin == SUPPLIER_KA
    assert inv.invoice_number == "STS/25-26/0142"
    assert inv.invoice_date == date(2025, 10, 15)
    assert inv.place_of_supply_code == "29"
    assert (inv.taxable_value, inv.cgst, inv.sgst, inv.total_invoice_value) == (
        D("150000.00"), D("13500.00"), D("13500.00"), D("177000.00"))
    assert [li.hsn_sac for li in inv.line_items] == ["8471", "998314"]
    assert inv.line_items[1].quantity == D("40") and inv.line_items[1].gst_rate == D("18")
    assert result.field_sources["supplier_gstin"] is FieldSource.PDF_TEXT


def test_text_pdf_extraction_inter_state_with_round_off(state_codes):
    inv = ExtractionService(None, None, state_codes).extract(render_pdf(valid_inter_state()), "pdf").invoice
    assert inv.igst == D("10080.90") and inv.round_off == D("0.10") and inv.cgst is None
    assert inv.place_of_supply_code == "24"


def test_missing_fields_are_not_fabricated(state_codes):
    result = ExtractionService(None, None, state_codes).extract(render_pdf(missing_fields_invoice()), "pdf")
    assert result.status is ExtractionStatus.PARTIAL
    assert result.invoice.invoice_number is None
    assert result.invoice.recipient_gstin is None
    assert result.invoice.place_of_supply is None
    assert {"invoice_number", "recipient_gstin", "place_of_supply"} <= set(result.missing_fields)
    # The line without an HSN code is kept, with the code left empty.
    assert [li.hsn_sac for li in result.invoice.line_items] == ["9403", None]


def test_invalid_values_are_extracted_as_printed(state_codes):
    inv = ExtractionService(None, None, state_codes).extract(render_pdf(invalid_invoice()), "pdf").invoice
    assert inv.supplier_gstin != SUPPLIER_KA  # wrong checksum kept as-is for the validator
    assert inv.line_items[0].gst_rate == D("12")


def test_ocr_extraction_from_png(ocr_engine, state_codes):
    png = pdf_to_png(render_pdf(valid_intra_state()), dpi=200)
    result = ExtractionService(ocr_engine, None, state_codes).extract(png, "png")
    inv = result.invoice
    assert result.method == "ocr"
    assert inv.supplier_gstin == SUPPLIER_KA
    assert inv.total_invoice_value == D("177000.00")
    assert len(inv.line_items) == 2


def test_scanned_pdf_uses_ocr(ocr_engine, state_codes):
    scanned = png_to_scanned_pdf(pdf_to_png(render_pdf(valid_intra_state()), dpi=200))
    result = ExtractionService(ocr_engine, None, state_codes).extract(scanned, "pdf")
    assert result.method == "ocr"
    assert result.invoice.invoice_number == "STS/25-26/0142"


def test_ocr_unavailable_for_scanned_pdf(state_codes):
    scanned = png_to_scanned_pdf(pdf_to_png(render_pdf(valid_intra_state()), dpi=72))
    result = ExtractionService(None, None, state_codes).extract(scanned, "pdf")
    assert result.status is ExtractionStatus.FAILED
    assert "No OCR engine" in result.error


def test_ocr_engine_crash_is_handled(state_codes):
    png = pdf_to_png(render_pdf(valid_intra_state()), dpi=72)
    result = ExtractionService(BrokenOCR(), None, state_codes).extract(png, "png")
    assert result.status is ExtractionStatus.FAILED
    assert "OCR failed" in result.error


def test_blank_image_yields_failed_extraction(state_codes):
    png = pdf_to_png(png_to_scanned_pdf(pdf_to_png(render_pdf(valid_intra_state()), dpi=72)), dpi=72)
    result = ExtractionService(EmptyOCR(), None, state_codes).extract(png, "png")
    assert result.status is ExtractionStatus.FAILED
    assert "No readable text" in result.error


def test_corrupted_bytes_fail_gracefully(state_codes):
    result = ExtractionService(None, None, state_codes).extract(b"%PDF-1.4 not really a pdf", "pdf")
    assert result.status is ExtractionStatus.FAILED


def test_low_confidence_ocr_lines_mark_fields_uncertain(state_codes):
    lines = [TextLine("Invoice No: INV-77", 0.95), TextLine(f"GSTIN: {SUPPLIER_KA}", 0.60)]
    parsed = InvoiceParser(state_codes).parse(lines)
    assert parsed.invoice.supplier_gstin == SUPPLIER_KA
    assert "supplier_gstin" in parsed.uncertain_fields
    assert "invoice_number" not in parsed.uncertain_fields


def test_restore_word_spaces():
    assert restore_word_spaces("TaxableValue") == "Taxable Value"
    assert restore_word_spaces("GSTIN:29AAQCS4821K1ZQ") == "GSTIN:29AAQCS4821K1ZQ"


def test_segments_grouped_into_lines():
    from app.services.ocr_service import segments_to_lines

    segs = [TextSegment("Total", 0, 100, 40, 110), TextSegment("1,000.00", 200, 101, 260, 111),
            TextSegment("Next line", 0, 130, 60, 140)]
    lines = segments_to_lines(segs, column_gap=10)
    assert [l.text for l in lines] == ["Total  1,000.00", "Next line"]


# --- LLM-assisted extraction must stay grounded in the document text --------
class FakeExtractLLM:
    model = "fake"

    def __init__(self, data):
        self.data = data

    def complete_json(self, system, user, schema):
        return self.data


def test_llm_extraction_only_accepts_grounded_values():
    text = "ACME TRADERS\nBill No. 55/A dated 12.10.2025\nGrand amt 1,180.00"
    invoice = InvoiceData()
    llm = FakeExtractLLM({
        "invoice_number": "55/A",           # present in text -> accepted
        "total_invoice_value": "1,180.00",  # present -> accepted
        "supplier_gstin": "29ABCDE1234F1Z5",  # NOT present -> rejected (hallucination)
        "invoice_date": None,
    })
    filled, rejected = fill_missing_fields(invoice, text, llm)
    assert set(filled) == {"invoice_number", "total_invoice_value"}
    assert rejected == ["supplier_gstin"]
    assert invoice.supplier_gstin is None
    assert invoice.total_invoice_value == D("1180.00")


def test_is_grounded_ignores_spacing_and_commas():
    assert is_grounded("1,77,000.00", "Total 177000.00")
    assert not is_grounded("27000", "Total 177000.00".replace("177000", "1770"))

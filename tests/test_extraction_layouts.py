"""Unusual layouts, multi-page and multi-rate invoices, discounts, OCR vs PDF-text labelling."""

from decimal import Decimal

import pytest

from app.models.validation import CheckStatus, OverallStatus
from app.services.extraction_service import ExtractionService
from app.services.invoice_parser import InvoiceParser, _column_roles
from app.services.ocr_service import TextLine, get_ocr_engine
from app.utils.sample_invoice import (
    RECIPIENT_KA,
    SUPPLIER_KA,
    discount_invoice,
    multi_rate_multipage_invoice,
    pdf_to_png,
    render_pdf,
    valid_intra_state,
)
from tests.conftest import TODAY, checks_by_id

D = Decimal


@pytest.fixture(scope="module")
def ocr_engine():
    engine = get_ocr_engine("auto")
    if engine is None:
        pytest.skip("No OCR engine installed")
    return engine


def _parse(state_codes, *texts, confidence=1.0):
    return InvoiceParser(state_codes).parse([TextLine(t, confidence) for t in texts])


def test_multi_page_multi_rate_invoice(engine):
    extraction = ExtractionService(None, None).extract(render_pdf(multi_rate_multipage_invoice()), "pdf")
    inv = extraction.invoice
    assert extraction.page_count == 2
    assert len(inv.line_items) == 5  # rows from both pages; the repeated header is not an item
    assert [li.gst_rate for li in inv.line_items] == [D("18"), D("18"), D("5"), D("18"), D("5")]
    assert inv.cgst == inv.sgst == D("5860.00")  # 5,760 (9%) + 100 (2.5%) rows added together
    assert any("2 rows" in w for w in extraction.warnings)
    result = engine.run(extraction, today=TODAY)
    checks = checks_by_id(result)
    for check_id in ("calc.cgst", "calc.sgst", "calc.total_tax", "calc.invoice_total",
                     "calc.invoice_total_expected"):
        assert checks[check_id].status is CheckStatus.PASS, check_id
    # The 5% medicine lines use a low-confidence reference entry: review, never a silent PASS.
    assert checks["hsn.line3.rate"].status is CheckStatus.MANUAL_REVIEW
    assert result.overall_status is OverallStatus.NEEDS_REVIEW


def test_discount_invoice_uses_taxable_value_not_sub_total(engine):
    extraction = ExtractionService(None, None).extract(render_pdf(discount_invoice()), "pdf")
    inv = extraction.invoice
    assert inv.discount == D("1000.00")
    assert inv.taxable_value == D("34000.00")  # not the pre-discount Sub Total of 35,000
    result = engine.run(extraction, today=TODAY)
    checks = checks_by_id(result)
    assert checks["calc.taxable_total"].status is CheckStatus.PASS
    assert checks["calc.igst"].status is CheckStatus.PASS
    assert result.overall_status is OverallStatus.VALID


def test_table_header_maps_discount_and_tax_amount_columns(state_codes):
    assert _column_roles("Item  HSN  Qty  Price  Disc  Taxable Amt  CGST Amt  SGST Amt  Total") == [
        "quantity", "unit_price", "discount", "taxable_value", "tax_amount", "tax_amount", "line_total"]
    result = _parse(state_codes,
                    "Item  HSN  Qty  Price  Disc  Taxable Amt  GST %",
                    "Office chair  9403  2  6,000.00  500.00  11,500.00  18%")
    item = result.invoice.line_items[0]
    assert (item.quantity, item.unit_price, item.discount, item.taxable_value, item.gst_rate) == (
        D("2"), D("6000.00"), D("500.00"), D("11500.00"), D("18"))
    assert "line_items" not in result.uncertain_fields


def test_rows_that_do_not_match_the_header_are_flagged_uncertain(state_codes):
    result = _parse(state_codes,
                    "Description  HSN/SAC  Qty  Rate  Taxable Value  GST %",
                    "Office chair  9403  2  6,000.00  500.00  11,500.00  2,070.00  18%")
    assert result.invoice.line_items
    assert "line_items" in result.uncertain_fields  # never silently treated as verified


def test_unusual_labels(state_codes):
    inv = _parse(state_codes,
                 "Sahyadri Tech Solutions Pvt Ltd",
                 f"GST No: {SUPPLIER_KA}",
                 "Tax Invoice No. STS-0991",
                 "Dated: 05.11.2025",
                 "Buyer: Kaveri Retail Traders",
                 f"GSTIN/UIN: {RECIPIENT_KA}",
                 "Place of Supply : Karnataka").invoice
    assert inv.supplier_gstin == SUPPLIER_KA and inv.recipient_gstin == RECIPIENT_KA
    assert inv.invoice_number == "STS-0991"
    assert str(inv.invoice_date) == "2025-11-05"
    assert inv.place_of_supply_code == "29"
    assert inv.recipient_name == "Kaveri Retail Traders"


def test_missing_fields_stay_missing_in_unusual_layout(state_codes):
    inv = _parse(state_codes, "Some Trader", "Item  HSN  Qty  Amount", "Widget  8471  1  500.00").invoice
    assert inv.supplier_gstin is None and inv.invoice_number is None and inv.total_invoice_value is None


def test_low_confidence_values_are_not_treated_as_verified(engine):
    extraction = ExtractionService(None, None).extract(render_pdf(valid_intra_state()), "pdf")
    extraction = extraction.model_copy(update={"uncertain_fields": ["cgst"]})
    result = engine.run(extraction, today=TODAY)
    assert checks_by_id(result)["fields.cgst.uncertain"].status is CheckStatus.MANUAL_REVIEW
    assert result.overall_status is OverallStatus.NEEDS_REVIEW


def test_pdf_text_extraction_is_labelled_as_no_ocr():
    extraction = ExtractionService(None, None).extract(render_pdf(valid_intra_state()), "pdf")
    assert extraction.method == "pdf_text" and extraction.ocr_used is False
    assert extraction.ocr_pages == []
    assert "no OCR" in extraction.method_label
    assert extraction.model_dump()["method_label"] == extraction.method_label  # included in JSON


def test_scanned_image_is_labelled_as_ocr(ocr_engine, engine):
    png = pdf_to_png(render_pdf(valid_intra_state()))
    extraction = ExtractionService(ocr_engine, None).extract(png, "png")
    assert extraction.method == "ocr" and extraction.ocr_used is True
    assert extraction.ocr_pages == [1]
    assert extraction.method_label.startswith(f"OCR ({ocr_engine.name})")
    assert all(src.value == "ocr" for src in extraction.field_sources.values())
    result = engine.run(extraction, today=TODAY)
    assert checks_by_id(result)["calc.invoice_total_expected"].status is CheckStatus.PASS


def test_concurrent_extractions_do_not_share_parser_state():
    # Uploads are extracted in parallel threads; per-document parser state (table columns, per-rate tax
    # rows) must not leak between two invoices processed at the same time by one service.
    from concurrent.futures import ThreadPoolExecutor

    service = ExtractionService(None, None)
    multi, single = render_pdf(multi_rate_multipage_invoice()), render_pdf(discount_invoice())

    def run(i):
        pdf, expected = (multi, (5, D("5860.00"), None)) if i % 2 else (single, (2, None, D("6120.00")))
        inv = service.extract(pdf, "pdf").invoice
        return (len(inv.line_items), inv.cgst, inv.igst) == expected

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(pool.map(run, range(40)))


def test_large_pdf_page_is_rendered_at_reduced_dpi():
    import pymupdf

    from app.services.document_reader import _render_dpi

    doc = pymupdf.open()
    huge = doc.new_page(width=72 * 100, height=72 * 100)  # 100 x 100 inch page
    assert _render_dpi(huge, 200, 40_000_000) < 200
    normal = doc.new_page(width=595, height=842)
    assert _render_dpi(normal, 200, 40_000_000) == 200

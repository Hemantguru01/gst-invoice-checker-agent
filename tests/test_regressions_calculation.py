"""Regression tests for the invoice-total / expected-tax audit findings."""

from decimal import Decimal

from app.models.invoice import LineItem
from app.models.validation import CheckStatus, OverallStatus
from app.services.extraction_service import ExtractionService
from app.utils.money import parse_amount
from app.utils.sample_invoice import invalid_invoice, render_pdf, valid_inter_state, valid_intra_state
from app.validators.engine import _dedupe
from app.validators.base import make_check
from app.models.validation import CheckCategory
from tests.conftest import TODAY, as_extraction, checks_by_id, intra_state_invoice

D = Decimal


def run(engine, invoice):
    return engine.run(as_extraction(invoice), today=TODAY)


def _amount(text: str | None) -> Decimal:
    value = parse_amount(text)
    assert value is not None, text
    return value


def _107000_invoice():
    """The audited example: taxable ₹1,07,000, line 1 at 12%, line 2 at 18%, SGST short by ₹80."""
    return intra_state_invoice(
        line_items=[
            LineItem(description="Laptop", hsn_sac="8471", quantity=D("2"), unit_price=D("45000"),
                     taxable_value=D("95000"), gst_rate=D("12")),
            LineItem(description="IT support", hsn_sac="998313", quantity=D("10"), unit_price=D("1200"),
                     taxable_value=D("12000"), gst_rate=D("18")),
        ],
        taxable_value=D("107000"), cgst=D("6780"), sgst=D("6700"), total_invoice_value=D("125000"),
    )


def test_107000_example_expected_cgst_sgst_and_total(engine):
    checks = checks_by_id(run(engine, _107000_invoice()))
    assert checks["calc.cgst"].expected == "₹6,780.00"
    assert checks["calc.sgst"].expected == "₹6,780.00"
    assert checks["calc.total_tax"].expected == "₹13,560.00"
    expected_total = checks["calc.invoice_total_expected"]
    assert expected_total.status is CheckStatus.FAIL
    assert expected_total.expected == "₹1,20,560.00"  # 1,07,000 + 6,780 + 6,780, not ₹1,20,480
    assert "difference ₹4,440.00" in expected_total.message
    assert "expected GST ₹13,560.00" in expected_total.message


def test_107000_example_printed_components_check_is_separate_and_labelled(engine):
    checks = checks_by_id(run(engine, _107000_invoice()))
    printed = checks["calc.invoice_total"]
    assert printed.name == "Invoice total vs printed components"
    assert printed.status is CheckStatus.FAIL
    assert printed.expected == "₹1,20,480.00"  # 1,07,000 + printed 6,780 + printed 6,700
    assert "printed CGST ₹6,780.00 + SGST ₹6,700.00" in printed.message
    assert checks["calc.invoice_total_expected"].name == "Invoice total vs expected GST"


def test_107000_example_from_the_real_sample_pdf(engine):
    extraction = ExtractionService(None, None).extract(render_pdf(invalid_invoice()), "pdf")
    checks = checks_by_id(engine.run(extraction, today=TODAY))
    assert checks["calc.cgst"].expected == checks["calc.sgst"].expected == "₹6,780.00"
    assert checks["calc.invoice_total_expected"].expected == "₹1,20,560.00"
    assert checks["calc.invoice_total"].expected == "₹1,20,480.00"


def test_expected_total_is_built_from_the_same_figures_as_the_tax_checks(engine):
    for invoice in (_107000_invoice(), intra_state_invoice(), intra_state_invoice(cgst=D("12000"), sgst=D("12000"))):
        checks = checks_by_id(run(engine, invoice))
        taxable = sum(li.taxable_value for li in invoice.line_items)
        cgst, sgst = _amount(checks["calc.cgst"].expected), _amount(checks["calc.sgst"].expected)
        assert _amount(checks["calc.total_tax"].expected) == cgst + sgst
        assert _amount(checks["calc.invoice_total_expected"].expected) == taxable + cgst + sgst
        actual = _amount(checks["calc.invoice_total_expected"].actual)
        assert actual == invoice.total_invoice_value


def test_expected_total_igst_and_round_off(engine, hsn_ref, state_codes):
    extraction = ExtractionService(None, None).extract(render_pdf(valid_inter_state()), "pdf")
    checks = checks_by_id(engine.run(extraction, today=TODAY))
    assert checks["calc.invoice_total_expected"].status is CheckStatus.PASS
    assert "round-off ₹0.10" in checks["calc.invoice_total_expected"].message
    assert _amount(checks["calc.igst"].expected) == D("10080.90")


def test_valid_samples_still_pass_both_total_checks(engine):
    for factory in (valid_intra_state, valid_inter_state):
        extraction = ExtractionService(None, None).extract(render_pdf(factory()), "pdf")
        checks = checks_by_id(engine.run(extraction, today=TODAY))
        assert checks["calc.invoice_total"].status is CheckStatus.PASS
        assert checks["calc.invoice_total_expected"].status is CheckStatus.PASS


def test_tax_is_never_checked_against_a_rate_derived_from_itself(engine):
    # Single line, no printed rate, code unknown to the reference: the old code derived the rate from the
    # printed tax and then "verified" that same tax. Now nothing can be verified -> manual review.
    invoice = intra_state_invoice(
        line_items=[LineItem(description="Steel kitchen rack", hsn_sac="7323", quantity=D("10"),
                             unit_price=D("1000"), taxable_value=D("10000"))],
        taxable_value=D("10000"), cgst=D("700"), sgst=D("700"), total_invoice_value=D("11400"))
    result = run(engine, invoice)
    checks = checks_by_id(result)
    assert checks["calc.tax_amount"].status is CheckStatus.MANUAL_REVIEW
    assert "calc.cgst" not in checks
    assert checks["calc.invoice_total_expected"].status is CheckStatus.MANUAL_REVIEW
    assert checks["calc.invoice_total"].status is CheckStatus.PASS  # printed figures do add up
    assert result.overall_status is not OverallStatus.VALID


def test_missing_rate_uses_only_high_confidence_reference_rate(engine):
    # 8471 is high-confidence 18%: the reference rate is used (and stated as an assumption), so a
    # 14% charge is caught.
    invoice = intra_state_invoice(
        line_items=[LineItem(description="Laptop", hsn_sac="8471", quantity=D("1"), unit_price=D("50000"),
                             taxable_value=D("50000"))],
        taxable_value=D("50000"), cgst=D("3500"), sgst=D("3500"), total_invoice_value=D("57000"))
    result = run(engine, invoice)
    checks = checks_by_id(result)
    assert checks["calc.total_tax"].status is CheckStatus.FAIL
    assert checks["calc.total_tax"].expected == "₹9,000.00"
    assert any("not legally authoritative" in a for a in result.assumptions)


def test_missing_rate_with_low_confidence_reference_is_not_estimated(engine):
    invoice = intra_state_invoice(
        line_items=[LineItem(description="Rice 25kg", hsn_sac="1006", quantity=D("10"), unit_price=D("1000"),
                             taxable_value=D("10000"))],
        taxable_value=D("10000"), cgst=D("250"), sgst=D("250"), total_invoice_value=D("10500"))
    checks = checks_by_id(run(engine, invoice))
    assert checks["calc.tax_amount"].status is CheckStatus.MANUAL_REVIEW


def test_invoice_level_discount_single_rate(engine):
    invoice = intra_state_invoice(discount=D("10000"), taxable_value=D("140000"), cgst=D("12600"),
                                  sgst=D("12600"), total_invoice_value=D("165200"))
    checks = checks_by_id(run(engine, invoice))
    assert checks["calc.taxable_total"].status is CheckStatus.PASS
    assert checks["calc.cgst"].status is CheckStatus.PASS
    assert checks["calc.invoice_total_expected"].status is CheckStatus.PASS


def test_invoice_level_discount_with_several_rates_needs_review(engine):
    items = intra_state_invoice().line_items
    items[1] = items[1].model_copy(update={"gst_rate": D("5")})
    invoice = intra_state_invoice(line_items=items, discount=D("1000"), taxable_value=D("149000"))
    checks = checks_by_id(run(engine, invoice))
    assert checks["calc.tax_amount"].status is CheckStatus.MANUAL_REVIEW
    assert "discount" in checks["calc.tax_amount"].message


def test_line_discount_is_part_of_line_arithmetic(engine):
    items = intra_state_invoice().line_items
    items[0] = items[0].model_copy(update={"discount": D("5000"), "taxable_value": D("85000")})
    checks = checks_by_id(run(engine, intra_state_invoice(line_items=items)))
    assert checks["calc.line1.value"].status is CheckStatus.PASS
    assert "discount" in checks["calc.line1.value"].message


def test_dedupe_keeps_the_most_severe_result():
    passed = make_check("x.y", CheckCategory.CALCULATION, "X", CheckStatus.PASS, "ok")
    failed = make_check("x.y", CheckCategory.CALCULATION, "X", CheckStatus.FAIL, "bad")
    assert _dedupe([passed, failed])[0].status is CheckStatus.FAIL
    assert _dedupe([failed, passed])[0].status is CheckStatus.FAIL


def test_expected_and_found_use_the_same_indian_format_as_the_message(engine):
    check = checks_by_id(run(engine, intra_state_invoice(total_invoice_value=D("180000"))))["calc.invoice_total"]
    assert check.expected == "₹1,77,000.00" and check.actual == "₹1,80,000.00"
    assert check.expected in check.message and check.actual in check.message

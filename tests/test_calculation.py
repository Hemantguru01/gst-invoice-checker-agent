from decimal import Decimal

from app.models.invoice import LineItem
from app.models.validation import CheckStatus, OverallStatus
from tests.conftest import TODAY, as_extraction, checks_by_id, inter_state_invoice, intra_state_invoice

D = Decimal


def run(engine, invoice):
    return engine.run(as_extraction(invoice), today=TODAY)


def test_valid_intra_state_invoice_passes_everything(engine):
    result = run(engine, intra_state_invoice())
    failing = [c for c in result.checks if c.status is not CheckStatus.PASS]
    assert failing == []
    assert result.overall_status is OverallStatus.VALID


def test_cgst_sgst_correct(engine):
    checks = checks_by_id(run(engine, intra_state_invoice()))
    assert checks["supply.type"].status is CheckStatus.PASS
    assert "intra-state" in checks["supply.type"].message
    assert checks["calc.cgst"].status is CheckStatus.PASS
    assert checks["calc.sgst"].status is CheckStatus.PASS
    assert checks["calc.cgst_sgst_equal"].status is CheckStatus.PASS


def test_cgst_sgst_mismatch(engine):
    result = run(engine, intra_state_invoice(sgst=D("13000"), total_invoice_value=D("176500")))
    checks = checks_by_id(result)
    assert checks["calc.cgst_sgst_equal"].status is CheckStatus.FAIL
    assert checks["calc.sgst"].status is CheckStatus.FAIL
    assert checks["calc.cgst"].status is CheckStatus.PASS
    assert result.overall_status is OverallStatus.INVALID


def test_incorrect_gst_amount(engine):
    # 18% of 1,50,000 is 27,000; invoice charges 24,000 (as if 16%).
    result = run(engine, intra_state_invoice(cgst=D("12000"), sgst=D("12000"), total_invoice_value=D("174000")))
    checks = checks_by_id(result)
    assert checks["calc.total_tax"].status is CheckStatus.FAIL
    assert checks["calc.total_tax"].expected == "₹27,000.00"
    # The printed total adds up on its own figures...
    assert checks["calc.invoice_total"].status is CheckStatus.PASS
    # ...but not against the GST that is actually due.
    assert checks["calc.invoice_total_expected"].status is CheckStatus.FAIL
    assert checks["calc.invoice_total_expected"].expected == "₹1,77,000.00"


def test_igst_correct_for_inter_state(engine):
    result = run(engine, inter_state_invoice())
    checks = checks_by_id(result)
    assert "inter-state" in checks["supply.type"].message
    assert checks["calc.tax_type"].status is CheckStatus.PASS
    assert checks["calc.igst"].status is CheckStatus.PASS
    assert result.overall_status is OverallStatus.VALID


def test_igst_wrong_amount(engine):
    checks = checks_by_id(run(engine, inter_state_invoice(igst=D("6000"), total_invoice_value=D("43500"))))
    assert checks["calc.igst"].status is CheckStatus.FAIL


def test_cgst_sgst_charged_on_inter_state_supply(engine):
    invoice = inter_state_invoice(igst=None, cgst=D("3375"), sgst=D("3375"))
    checks = checks_by_id(run(engine, invoice))
    assert checks["calc.tax_type"].status is CheckStatus.FAIL
    assert checks["calc.tax_type"].expected == "IGST"


def test_igst_charged_on_intra_state_supply(engine):
    invoice = intra_state_invoice(cgst=None, sgst=None, igst=D("27000"))
    checks = checks_by_id(run(engine, invoice))
    assert checks["calc.tax_type"].status is CheckStatus.FAIL


def test_incorrect_invoice_total(engine):
    result = run(engine, intra_state_invoice(total_invoice_value=D("180000")))
    check = checks_by_id(result)["calc.invoice_total"]
    assert check.status is CheckStatus.FAIL
    assert check.expected == "₹1,77,000.00"
    assert checks_by_id(result)["calc.invoice_total_expected"].status is CheckStatus.FAIL
    assert result.overall_status is OverallStatus.INVALID


def test_rounding_difference_within_tolerance(engine):
    invoice = intra_state_invoice(total_invoice_value=D("177000.40"))
    check = checks_by_id(run(engine, invoice))["calc.invoice_total"]
    assert check.status is CheckStatus.PASS
    assert "rounding" in check.message


def test_round_off_line_is_used(engine):
    invoice = inter_state_invoice(
        line_items=[LineItem(description="SSD", hsn_sac="8523", quantity=D("10"), unit_price=D("1850.50"),
                             taxable_value=D("18505"), gst_rate=D("18"))],
        taxable_value=D("18505"), igst=D("3330.90"), round_off=D("0.10"), total_invoice_value=D("21836"))
    checks = checks_by_id(run(engine, invoice))
    assert checks["calc.invoice_total"].status is CheckStatus.PASS
    assert "round-off" in checks["calc.invoice_total"].message


def test_line_quantity_times_price_mismatch(engine):
    items = intra_state_invoice().line_items
    items[0] = items[0].model_copy(update={"taxable_value": D("95000")})
    checks = checks_by_id(run(engine, intra_state_invoice(line_items=items)))
    assert checks["calc.line1.value"].status is CheckStatus.FAIL
    assert checks["calc.taxable_total"].status is CheckStatus.FAIL


def test_missing_place_of_supply_needs_review(engine):
    result = run(engine, intra_state_invoice(place_of_supply=None, place_of_supply_code=None))
    checks = checks_by_id(result)
    assert checks["supply.type"].status is CheckStatus.MANUAL_REVIEW
    assert result.overall_status is OverallStatus.NEEDS_REVIEW
    assert any("inferred" in a for a in result.assumptions)

from datetime import date
from decimal import Decimal

from app.models.invoice import LineItem
from app.models.validation import CheckStatus
from app.validators.hsn import check_hsn
from tests.conftest import checks_by_id, intra_state_invoice

D = Decimal


def _single_line(**item):
    base = dict(description="Item", hsn_sac="8471", quantity=D("1"), unit_price=D("1000"),
                taxable_value=D("1000"), gst_rate=D("18"))
    base.update(item)
    return intra_state_invoice(line_items=[LineItem(**base)], taxable_value=D("1000"),
                               cgst=D("90"), sgst=D("90"), total_invoice_value=D("1180"))


def test_reference_metadata_is_versioned(hsn_ref):
    assert hsn_ref.version
    assert hsn_ref.effective_from == date(2025, 9, 22)
    assert "NOT LEGALLY AUTHORITATIVE" in hsn_ref.legal_status


def test_longest_prefix_lookup(hsn_ref):
    assert hsn_ref.lookup("84713010").code == "8471"
    assert hsn_ref.lookup("998314").code == "998314"
    assert hsn_ref.lookup("12345678") is None


def test_correct_code_and_rate_pass(hsn_ref):
    checks = checks_by_id(check_hsn(_single_line(), hsn_ref))
    assert checks["hsn.line1.code"].status is CheckStatus.PASS
    assert checks["hsn.line1.rate"].status is CheckStatus.PASS


def test_incorrect_rate_fails(hsn_ref):
    checks = checks_by_id(check_hsn(_single_line(gst_rate=D("12")), hsn_ref))
    check = checks["hsn.line1.rate"]
    assert check.status is CheckStatus.FAIL
    assert "18" in check.message and "12" in check.message


def test_malformed_code_fails(hsn_ref):
    checks = checks_by_id(check_hsn(_single_line(hsn_sac="84A1"), hsn_ref))
    assert checks["hsn.line1.code"].status is CheckStatus.FAIL
    checks = checks_by_id(check_hsn(_single_line(hsn_sac="84711"), hsn_ref))  # 5 digits
    assert checks["hsn.line1.code"].status is CheckStatus.FAIL


def test_unknown_code_warns_and_skips_rate(hsn_ref):
    checks = checks_by_id(check_hsn(_single_line(hsn_sac="7323"), hsn_ref))
    assert checks["hsn.line1.code"].status is CheckStatus.WARNING
    assert "not in the local reference dataset" in checks["hsn.line1.code"].message
    assert "hsn.line1.rate" not in checks


def test_missing_code_detected(hsn_ref):
    checks = checks_by_id(check_hsn(_single_line(hsn_sac=None), hsn_ref))
    assert checks["hsn.line1.code"].status is CheckStatus.MISSING


def test_low_confidence_entry_goes_to_manual_review(hsn_ref):
    # 8415 is marked low-confidence in the reference data (rate changed in 2025).
    checks = checks_by_id(check_hsn(_single_line(hsn_sac="8415"), hsn_ref))
    assert checks["hsn.line1.rate"].status is CheckStatus.MANUAL_REVIEW


def test_invoice_before_reference_effective_date_needs_review(hsn_ref):
    invoice = _single_line().model_copy(update={"invoice_date": date(2024, 4, 1)})
    checks = checks_by_id(check_hsn(invoice, hsn_ref))
    assert checks["hsn.line1.rate"].status is CheckStatus.MANUAL_REVIEW
    assert "effective date" in checks["hsn.line1.rate"].message


def test_rate_derived_from_tax_for_single_line(hsn_ref):
    checks = checks_by_id(check_hsn(_single_line(gst_rate=None), hsn_ref))
    assert checks["hsn.line1.rate"].status is CheckStatus.PASS
    assert "derived" in checks["hsn.line1.rate"].message


def test_non_standard_slab_warns(hsn_ref):
    checks = checks_by_id(check_hsn(_single_line(gst_rate=D("17")), hsn_ref))
    assert checks["hsn.line1.slab"].status is CheckStatus.WARNING

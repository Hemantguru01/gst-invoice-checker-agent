import pytest

from app.models.validation import CheckStatus
from app.utils.sample_invoice import SUPPLIER_KA, make_gstin
from app.validators.gstin import GSTINError, compute_checksum, validate_gstin
from app.validators.gstin_rules import check_gstins
from tests.conftest import as_extraction, checks_by_id, intra_state_invoice


@pytest.fixture
def codes(state_codes):
    return state_codes.codes


def test_checksum_algorithm_known_value():
    # 27AAPFU0939F1ZV is the sample GSTIN widely used in GST documentation and tutorials
    # to illustrate the checksum algorithm; used here only as an algorithm test vector.
    assert compute_checksum("27AAPFU0939F1Z") == "V"


def test_valid_gstin(codes):
    result = validate_gstin(SUPPLIER_KA, codes)
    assert result.is_valid
    assert result.state_code == "29"
    assert result.state_name == "Karnataka"
    assert result.pan == SUPPLIER_KA[2:12]


def test_gstin_is_normalised(codes):
    messy = f" {SUPPLIER_KA[:5].lower()} {SUPPLIER_KA[5:]} "
    assert validate_gstin(messy, codes).is_valid


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_gstin(codes, value):
    result = validate_gstin(value, codes)
    assert not result.is_valid
    assert result.error is GSTINError.MISSING


def test_wrong_length_is_format_error(codes):
    result = validate_gstin(SUPPLIER_KA[:-1], codes)
    assert result.error is GSTINError.LENGTH


@pytest.mark.parametrize("bad", [
    "29AAQC54821K1ZQ",   # digit where PAN letter expected
    "2AAAQCS4821K1ZQ",   # state code not numeric
    "29AAQCS4821K0ZQ",   # entity code 0
    "29AAQCS4821K1Z@",   # symbol
])
def test_format_errors(codes, bad):
    result = validate_gstin(bad, codes)
    assert result.error is GSTINError.FORMAT


def test_unknown_state_code(codes):
    gstin = make_gstin("45", "AAQCS4821K")  # 45 is not a GST state code
    result = validate_gstin(gstin, codes)
    assert result.error is GSTINError.STATE_CODE


def test_checksum_error_is_distinct_from_format_error(codes):
    bad = SUPPLIER_KA[:-1] + ("A" if SUPPLIER_KA[-1] != "A" else "B")
    result = validate_gstin(bad, codes)
    assert result.error is GSTINError.CHECKSUM
    assert result.expected_checksum == SUPPLIER_KA[-1]
    assert "Checksum mismatch" in result.message


def test_rule_checks_report_format_vs_checksum(codes):
    bad_checksum = SUPPLIER_KA[:-1] + ("A" if SUPPLIER_KA[-1] != "A" else "B")
    out, _, _ = check_gstins(intra_state_invoice(supplier_gstin=bad_checksum), codes)
    checks = checks_by_id(out)
    assert checks["gstin.supplier.format"].status is CheckStatus.PASS
    assert checks["gstin.supplier.checksum"].status is CheckStatus.FAIL

    out, _, _ = check_gstins(intra_state_invoice(supplier_gstin="29AAQC54821K1ZQ"), codes)
    checks = checks_by_id(out)
    assert checks["gstin.supplier.format"].status is CheckStatus.FAIL
    assert checks["gstin.supplier.format"].message.startswith("Format error")
    assert "gstin.supplier.checksum" not in checks


def test_missing_supplier_gstin_makes_invoice_invalid(engine):
    result = engine.run(as_extraction(intra_state_invoice(supplier_gstin=None)))
    check = checks_by_id(result)["fields.supplier_gstin"]
    assert check.status is CheckStatus.MISSING
    assert result.overall_status.value == "INVALID"


def test_same_supplier_and_recipient_warns(codes):
    out, _, _ = check_gstins(intra_state_invoice(recipient_gstin=SUPPLIER_KA), codes)
    assert checks_by_id(out)["gstin.same_party"].status is CheckStatus.WARNING

"""Turns GSTIN validation results into report checks."""

from __future__ import annotations

from app.models.invoice import InvoiceData
from app.models.validation import CheckCategory as Cat
from app.models.validation import CheckStatus as St
from app.validators.base import RuleOutput, make_check
from app.validators.gstin import GSTINError, GSTINResult, validate_gstin

REGISTRATION_NOT_VERIFIED = ("not verified: no authorised live GST portal / GSP integration is configured, "
                             "so registration and active status are unknown")

# Characters OCR commonly confuses with each other.
_CONFUSABLE = {"O": "0", "0": "O", "I": "1", "1": "I", "L": "1", "S": "5", "5": "S", "B": "8", "8": "B",
               "Z": "2", "2": "Z", "G": "6", "6": "G"}


def ocr_alternatives(value: str, state_codes: dict[str, str]) -> list[str]:
    """Valid GSTINs that differ from `value` by one commonly confused character (never applied automatically)."""
    value = value.strip().upper()
    if len(value) != 15:
        return []
    found = []
    for i, ch in enumerate(value):
        alt = _CONFUSABLE.get(ch)
        if alt is None:
            continue
        candidate = value[:i] + alt + value[i + 1:]
        if validate_gstin(candidate, state_codes).is_valid:
            found.append(candidate)
    return found


def _ocr_misread_check(prefix: str, title: str, field: str, result: GSTINResult,
                       state_codes: dict[str, str]) -> RuleOutput | None:
    alternatives = ocr_alternatives(result.gstin or "", state_codes)
    if not alternatives:
        return None
    out = RuleOutput()
    out.add(make_check(
        f"{prefix}.ocr_misread", Cat.GSTIN, f"{title} possible OCR misread", St.MANUAL_REVIEW,
        f"'{result.gstin}' fails validation ({result.message}), but it was read by OCR and changing one "
        f"commonly confused character gives a valid GSTIN: {', '.join(alternatives)}. The value was not "
        "changed.", field=field, actual=result.gstin, expected=" or ".join(alternatives),
        explanation="OCR often confuses characters such as O/0, I/1, S/5 and B/8. The printed GSTIN may be "
                    "correct and the reading wrong, or the invoice may really carry an invalid GSTIN.",
        suggestion="Compare the GSTIN with the original document and correct the extracted value if it "
                   "was misread.",
    ))
    return out


def _party_checks(party: str, value: str | None, state_codes: dict[str, str],
                  from_ocr: bool = False) -> tuple[RuleOutput, GSTINResult]:
    out = RuleOutput()
    result = validate_gstin(value, state_codes)
    title = f"{party.capitalize()} GSTIN"
    prefix = f"gstin.{party}"
    field = f"{party}_gstin"

    if result.error is GSTINError.MISSING:
        # Missing GSTINs are reported by the field-presence rules.
        return out, result
    if from_ocr and result.error in (GSTINError.FORMAT, GSTINError.STATE_CODE, GSTINError.CHECKSUM):
        misread = _ocr_misread_check(prefix, title, field, result, state_codes)
        if misread is not None:
            return misread, result

    if result.error in (GSTINError.LENGTH, GSTINError.FORMAT):
        out.add(make_check(
            f"{prefix}.format", Cat.GSTIN, f"{title} format", St.FAIL,
            f"Format error: {result.message}", field=field, actual=result.gstin,
            expected="2 digits + PAN (AAAAA9999A) + entity code + 'Z' + checksum",
            explanation="The GSTIN does not have the structure every GSTIN must follow, so it "
                        "cannot be a real GSTIN as written.",
            suggestion="Re-check each character against the supplier's registration certificate "
                       "or search it on the GST portal.",
        ))
        return out, result

    out.add(make_check(f"{prefix}.format", Cat.GSTIN, f"{title} format", St.PASS,
                       "Matches the 15-character GSTIN pattern.", field=field, actual=result.gstin))

    if result.error is GSTINError.STATE_CODE:
        out.add(make_check(
            f"{prefix}.state_code", Cat.GSTIN, f"{title} state code", St.FAIL,
            result.message, field=field, actual=result.state_code,
            explanation="The first two digits of a GSTIN identify the state or UT of registration; "
                        "this code does not exist.",
            suggestion="Verify the first two digits of the GSTIN.",
        ))
        return out, result

    out.add(make_check(f"{prefix}.state_code", Cat.GSTIN, f"{title} state code", St.PASS,
                       f"State code {result.state_code} = {result.state_name}.", field=field,
                       actual=result.state_code))

    if result.error is GSTINError.CHECKSUM:
        out.add(make_check(
            f"{prefix}.checksum", Cat.GSTIN, f"{title} checksum", St.FAIL,
            f"Checksum error: {result.message}", field=field, actual=result.gstin[14],
            expected=result.expected_checksum,
            explanation="The GSTIN looks structurally correct, but its last (check) character does "
                        "not match the rest. At least one character is wrong.",
            suggestion="Compare the GSTIN character by character with the supplier's registration "
                       "and verify it on the GST portal (Search Taxpayer).",
        ))
        return out, result

    out.add(make_check(f"{prefix}.checksum", Cat.GSTIN, f"{title} checksum", St.PASS,
                       "Checksum character is correct, so the GSTIN is well-formed. This does not show "
                       f"that it is registered or active (registration status: {REGISTRATION_NOT_VERIFIED}).",
                       field=field))
    if result.non_default_z:
        out.add(make_check(
            f"{prefix}.z_char", Cat.GSTIN, f"{title} 14th character", St.WARNING,
            f"14th character is '{result.gstin[13]}' instead of the usual 'Z'.", field=field,
            explanation="Almost all GSTINs have 'Z' in position 14. Other values are rare.",
            suggestion="Verify this GSTIN on the GST portal.",
        ))
    return out, result


def check_gstins(invoice: InvoiceData, state_codes: dict[str, str],
                 ocr_fields: set[str] | frozenset[str] = frozenset()) -> tuple[RuleOutput, GSTINResult, GSTINResult]:
    out = RuleOutput()
    supplier_out, supplier = _party_checks("supplier", invoice.supplier_gstin, state_codes,
                                           "supplier_gstin" in ocr_fields)
    recipient_out, recipient = _party_checks("recipient", invoice.recipient_gstin, state_codes,
                                             "recipient_gstin" in ocr_fields)
    out.extend(supplier_out)
    out.extend(recipient_out)

    if supplier.gstin and recipient.gstin and supplier.gstin == recipient.gstin:
        out.add(make_check(
            "gstin.same_party", Cat.GSTIN, "Supplier and recipient differ", St.WARNING,
            "Supplier and recipient have the same GSTIN.",
            explanation="An invoice is normally issued to a different registered person. "
                        "This can indicate an extraction error or a copy-paste mistake.",
            suggestion="Confirm which GSTIN belongs to the supplier and which to the recipient.",
        ))
    return out, supplier, recipient

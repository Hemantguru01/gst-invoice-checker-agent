"""HSN/SAC code and GST-rate checks against the local reference dataset."""

from __future__ import annotations

import re
from decimal import Decimal

from app.models.invoice import InvoiceData, LineItem
from app.models.validation import CheckCategory as Cat
from app.models.validation import CheckStatus as St
from app.models.validation import Severity
from app.services.reference_data import HSNEntry, HSNReference
from app.validators.base import RuleOutput, make_check

# Place-of-supply codes that mean the supply leaves the domestic intra/inter-state scheme.
SPECIAL_POS_CODES = {"96": "export (other country)", "97": "other territory"}


def is_special_supply(invoice: InvoiceData) -> bool:
    return bool(invoice.supply_indicators) or invoice.place_of_supply_code in SPECIAL_POS_CODES

# Slabs that have existed under GST (including pre-September-2025 slabs and special rates).
KNOWN_SLABS = {Decimal(x) for x in ("0", "0.1", "0.25", "1.5", "3", "5", "6", "12", "18", "28", "40")}


def code_format_problem(code: str) -> str | None:
    if not code.isdigit():
        return "HSN/SAC codes contain digits only."
    if code.startswith("99"):
        if len(code) not in (4, 6):
            return "SAC codes (starting with 99) should have 4 or 6 digits."
        return None
    if len(code) not in (2, 4, 6, 8):
        return "HSN codes should have 2, 4, 6 or 8 digits."
    return None


def applied_rate(item: LineItem, invoice: InvoiceData) -> tuple[Decimal | None, str | None]:
    """Rate printed on the line or, for single-line invoices, derived from the tax amounts."""
    if item.gst_rate is not None:
        return item.gst_rate, "printed on the invoice line"
    if len(invoice.line_items) == 1 and invoice.taxable_value:
        taxes = [t for t in (invoice.cgst, invoice.sgst, invoice.igst) if t is not None]
        if taxes:
            derived = (sum(taxes, Decimal(0)) / invoice.taxable_value * 100).quantize(Decimal("0.01"))
            for slab in KNOWN_SLABS:
                if abs(derived - slab) <= Decimal("0.05"):
                    return slab, "derived from the tax amounts (no rate printed)"
            return derived, "derived from the tax amounts (no rate printed)"
    return None, None


def check_hsn(invoice: InvoiceData, reference: HSNReference) -> RuleOutput:
    out = RuleOutput()
    out.assumptions.append(
        f"GST rates are compared with the local reference dataset version {reference.version} "
        f"(effective {reference.effective_from.isoformat()}), which is not legally authoritative."
    )

    for index, item in enumerate(invoice.line_items, start=1):
        prefix = f"hsn.line{index}"
        name = f"Line {index}"
        desc = item.description or "item"

        if not item.hsn_sac:
            out.add(make_check(
                f"{prefix}.code", Cat.HSN, f"{name} HSN/SAC code", St.MISSING,
                f"No HSN/SAC code found for line {index} ({desc}).", severity=Severity.WARNING,
                field="line_items.hsn_sac",
                explanation="HSN/SAC codes are mandatory on B2B invoices (4 digits up to ₹5 crore "
                            "turnover, 6 digits above). They may be optional on some B2C invoices.",
                suggestion="Add the correct HSN (goods) or SAC (services) code for this item.",
            ))
            continue

        code = item.hsn_sac.strip()
        problem = code_format_problem(code)
        if problem:
            out.add(make_check(
                f"{prefix}.code", Cat.HSN, f"{name} HSN/SAC code", St.FAIL,
                f"'{code}' is not a valid HSN/SAC code: {problem}", field="line_items.hsn_sac",
                actual=code,
                explanation="The code does not follow the HSN/SAC numbering structure.",
                suggestion="Look up the correct code in the CBIC HSN/SAC directory.",
            ))
            continue

        entry = reference.lookup(code)
        if entry is None:
            out.add(make_check(
                f"{prefix}.code", Cat.HSN, f"{name} HSN/SAC code", St.MANUAL_REVIEW,
                f"Code {code} is not in the local reference dataset; its GST rate was not verified.",
                field="line_items.hsn_sac", actual=code,
                explanation="The tool only knows the codes in its reference file. An unknown code is "
                            "not necessarily wrong, but its GST rate was not verified.",
                suggestion="Verify the code and its rate on the CBIC website, and add it to "
                           "data/reference/hsn_sac_rates.json after verification.",
            ))
            continue

        out.add(make_check(
            f"{prefix}.code", Cat.HSN, f"{name} HSN/SAC code", St.PASS,
            f"{code} found in reference data ({entry.code}: {entry.description}).",
            field="line_items.hsn_sac", actual=code,
        ))
        effective = entry.effective_from or reference.effective_from
        before_effective = invoice.invoice_date is not None and invoice.invoice_date < effective
        out.extend(_rate_check(prefix, name, item, invoice, entry, before_effective, reference))
    return out


def _rate_check(prefix: str, name: str, item: LineItem, invoice: InvoiceData, entry: HSNEntry,
                before_effective: bool, reference: HSNReference) -> RuleOutput:
    out = RuleOutput()
    rate, rate_source = applied_rate(item, invoice)
    check_id = f"{prefix}.rate"
    title = f"{name} GST rate"
    effective = entry.effective_from or reference.effective_from
    expected = (f"{entry.gst_rate}% (reference {entry.code}, confidence: {entry.confidence}, effective "
                f"{effective.isoformat()}, notification: {entry.notification_ref or 'not linked'})")

    if rate is None:
        out.add(make_check(
            check_id, Cat.HSN, title, St.MANUAL_REVIEW,
            "No GST rate is printed for this line and it cannot be derived reliably.",
            field="line_items.gst_rate", expected=expected,
            explanation="Without a per-line rate the applied rate cannot be compared with the reference.",
            suggestion="Confirm the rate charged on this line.",
        ))
        return out
    if rate not in KNOWN_SLABS:
        out.add(make_check(
            f"{prefix}.slab", Cat.HSN, f"{name} rate slab", St.WARNING,
            f"Rate {rate}% ({rate_source}) is not a standard GST slab.", field="line_items.gst_rate",
            actual=f"{rate}%",
            explanation="GST is levied at fixed slab rates. An unusual rate often means a calculation "
                        "or reading error.",
            suggestion="Check the rate and tax amounts on the invoice.",
        ))

    if before_effective:
        out.add(make_check(
            check_id, Cat.HSN, title, St.MANUAL_REVIEW,
            f"Invoice is dated before this reference entry's effective date "
            f"({effective.isoformat()}); the applicable rate may differ.",
            field="line_items.gst_rate", expected=expected, actual=f"{rate}%",
            explanation="GST rates changed over time (notably on 22 September 2025). The reference "
                        "data only describes rates from its effective date onwards.",
            suggestion="Check the rate that applied on the invoice date in the relevant CBIC notification.",
        ))
        return out

    matches = rate == entry.gst_rate
    if is_special_supply(invoice) and not matches:
        out.add(make_check(
            check_id, Cat.HSN, title, St.MANUAL_REVIEW,
            f"Applied rate {rate}% ({rate_source}) differs from the domestic reference rate {entry.gst_rate}%, "
            "but the invoice is marked as an export / SEZ / LUT supply, which may be zero-rated.",
            field="line_items.gst_rate", expected=expected, actual=f"{rate}%",
            explanation="Zero-rated supplies (exports, supplies to SEZ units) can legitimately show 0% or "
                        "be made without payment of tax under a LUT or bond. The domestic rate does not apply "
                        "automatically.",
            suggestion="Confirm the zero-rating conditions (LUT/bond, SEZ authorisation, proof of export).",
        ))
        return out
    if entry.confidence != "high":
        note = f" Note: {entry.notes}" if entry.notes else ""
        out.add(make_check(
            check_id, Cat.HSN, title, St.MANUAL_REVIEW,
            f"Applied rate {rate}% ({rate_source}) {'matches' if matches else 'differs from'} the "
            f"reference rate {entry.gst_rate}%, but this reference entry is marked low-confidence.{note}",
            field="line_items.gst_rate", expected=expected, actual=f"{rate}%",
            explanation="The rate for this code depends on conditions or changed recently, so the tool "
                        "will not treat it as verified.",
            suggestion="Confirm the applicable rate in the CBIC rate notification for this code.",
        ))
    elif matches:
        out.add(make_check(
            check_id, Cat.HSN, title, St.PASS,
            f"Applied rate {rate}% ({rate_source}) matches the local reference rate "
            f"(dataset {reference.version}, not legally authoritative).",
            field="line_items.gst_rate", expected=expected, actual=f"{rate}%",
        ))
    else:
        out.add(make_check(
            check_id, Cat.HSN, title, St.FAIL,
            f"Applied rate {rate}% ({rate_source}) does not match the reference rate {entry.gst_rate}% "
            f"for {entry.code} ({entry.description}).",
            field="line_items.gst_rate", expected=expected, actual=f"{rate}%",
            explanation="Charging the wrong rate leads to short or excess payment of tax, and the "
                        "buyer's input tax credit may be disputed. A specific exemption or "
                        "notification could justify a different rate.",
            suggestion=f"Confirm whether {entry.gst_rate}% applies; if so, ask the supplier for a "
                       "corrected invoice or credit/debit note. If an exemption applies, document it.",
        ))
    return out

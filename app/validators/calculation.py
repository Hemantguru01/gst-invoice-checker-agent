"""GST arithmetic checks: line values, tax type, tax amounts and invoice total."""

from __future__ import annotations

from decimal import Decimal
from enum import Enum

from app.models.invoice import InvoiceData
from app.models.validation import CheckCategory as Cat
from app.models.validation import CheckStatus as St
from app.models.validation import Severity
from app.services.reference_data import HSNReference
from app.utils.money import fmt_inr, q2
from app.validators.base import RuleOutput, make_check
from app.validators.gstin import GSTINResult
from app.validators.hsn import applied_rate

ZERO = Decimal("0")


class SupplyType(str, Enum):
    INTRA_STATE = "intra-state"
    INTER_STATE = "inter-state"
    UNKNOWN = "unknown"


def _nz(value: Decimal | None) -> bool:
    return value is not None and value != ZERO


def determine_supply_type(invoice: InvoiceData, supplier: GSTINResult, state_names: dict[str, str],
                          out: RuleOutput) -> SupplyType:
    supplier_state = supplier.state_code if supplier.state_name else None
    pos = invoice.place_of_supply_code

    if supplier_state and pos:
        same = supplier_state == pos
        supply = SupplyType.INTRA_STATE if same else SupplyType.INTER_STATE
        out.add(make_check(
            "supply.type", Cat.SUPPLY, "Supply type", St.PASS,
            f"Supplier state {supplier_state} ({state_names.get(supplier_state)}) and place of supply "
            f"{pos} ({state_names.get(pos)}) → {supply.value} supply.",
            expected="CGST + SGST" if same else "IGST",
        ))
        out.assumptions.append(
            "Supply type is decided by comparing the supplier's GSTIN state code with the place of "
            "supply. Special cases (SEZ supplies, exports, deemed exports, specific place-of-supply "
            "rules for services) are not modelled."
        )
        return supply

    # Fall back to what was charged, but flag it.
    if _nz(invoice.igst) and not (_nz(invoice.cgst) or _nz(invoice.sgst)):
        guessed = SupplyType.INTER_STATE
    elif (_nz(invoice.cgst) or _nz(invoice.sgst)) and not _nz(invoice.igst):
        guessed = SupplyType.INTRA_STATE
    else:
        guessed = SupplyType.UNKNOWN
    missing = "place of supply" if not pos else "a valid supplier GSTIN state code"
    out.add(make_check(
        "supply.type", Cat.SUPPLY, "Supply type", St.MANUAL_REVIEW,
        f"Cannot determine intra/inter-state supply because {missing} is unavailable. "
        f"Tax charged suggests: {guessed.value}.",
        explanation="Whether CGST+SGST or IGST applies depends on the supplier's location and the "
                    "place of supply. Without both, the tax type cannot be verified.",
        suggestion="Confirm the place of supply (state name and code) printed on the invoice.",
    ))
    out.assumptions.append(f"Supply type inferred from the taxes charged ({guessed.value}); not verified.")
    return guessed


def check_calculations(invoice: InvoiceData, supplier: GSTINResult, reference: HSNReference,
                       state_names: dict[str, str], tolerance: Decimal) -> RuleOutput:
    out = RuleOutput()
    out.assumptions.append(
        f"Amounts within ₹{tolerance:.2f} of the expected value are treated as rounding differences."
    )

    _line_arithmetic(invoice, tolerance, out)
    supply = determine_supply_type(invoice, supplier, state_names, out)
    _tax_type(invoice, supply, out)
    _tax_amounts(invoice, supply, reference, tolerance, out)
    _invoice_total(invoice, tolerance, out)
    return out


def _line_arithmetic(invoice: InvoiceData, tolerance: Decimal, out: RuleOutput) -> None:
    for index, item in enumerate(invoice.line_items, start=1):
        if item.quantity is None or item.unit_price is None or item.taxable_value is None:
            continue
        expected = q2(item.quantity * item.unit_price)
        diff = abs(expected - item.taxable_value)
        if diff <= tolerance:
            out.add(make_check(
                f"calc.line{index}.value", Cat.CALCULATION, f"Line {index} quantity × price", St.PASS,
                f"{item.quantity} × {fmt_inr(item.unit_price)} = {fmt_inr(expected)}.",
                expected=expected, actual=item.taxable_value,
            ))
        else:
            out.add(make_check(
                f"calc.line{index}.value", Cat.CALCULATION, f"Line {index} quantity × price", St.FAIL,
                f"Line {index}: {item.quantity} × {fmt_inr(item.unit_price)} = {fmt_inr(expected)}, "
                f"but the invoice shows {fmt_inr(item.taxable_value)} (difference {fmt_inr(diff)}).",
                expected=expected, actual=item.taxable_value,
                explanation="The line value should equal quantity multiplied by unit price "
                            "(less any discount shown separately).",
                suggestion="Check the quantity, unit price and any discount on this line.",
            ))

    line_values = [i.taxable_value for i in invoice.line_items]
    if invoice.taxable_value is None or not line_values or any(v is None for v in line_values):
        return
    line_sum = q2(sum(line_values, ZERO))  # type: ignore[arg-type]
    diff = abs(line_sum - invoice.taxable_value)
    if diff <= tolerance:
        out.add(make_check(
            "calc.taxable_total", Cat.CALCULATION, "Taxable value = sum of lines", St.PASS,
            f"Sum of line values {fmt_inr(line_sum)} matches taxable value.",
            expected=line_sum, actual=invoice.taxable_value,
        ))
    else:
        out.add(make_check(
            "calc.taxable_total", Cat.CALCULATION, "Taxable value = sum of lines", St.FAIL,
            f"Sum of line values is {fmt_inr(line_sum)} but taxable value shows "
            f"{fmt_inr(invoice.taxable_value)} (difference {fmt_inr(diff)}).",
            expected=line_sum, actual=invoice.taxable_value,
            explanation="The invoice's taxable value should equal the total of its line items.",
            suggestion="Check for a missing line, an unlisted discount or a typing error.",
        ))


def _tax_type(invoice: InvoiceData, supply: SupplyType, out: RuleOutput) -> None:
    if supply is SupplyType.INTRA_STATE:
        if _nz(invoice.igst):
            out.add(make_check(
                "calc.tax_type", Cat.CALCULATION, "Correct tax type", St.FAIL,
                f"IGST of {fmt_inr(invoice.igst)} is charged on an intra-state supply.",
                expected="CGST + SGST", actual="IGST",
                explanation="When supplier and place of supply are in the same state, CGST and SGST "
                            "apply, not IGST. Tax paid under the wrong head is not automatically "
                            "adjusted.",
                suggestion="Ask the supplier to re-issue the invoice with CGST and SGST.",
            ))
        elif not (_nz(invoice.cgst) or _nz(invoice.sgst)):
            out.add(make_check(
                "calc.tax_type", Cat.CALCULATION, "Correct tax type", St.MANUAL_REVIEW,
                "No CGST/SGST found on an intra-state supply.",
                explanation="Either the supply is exempt/nil-rated or the tax amounts were not extracted.",
                suggestion="Confirm whether tax was charged and whether an exemption applies.",
            ))
        else:
            out.add(make_check("calc.tax_type", Cat.CALCULATION, "Correct tax type", St.PASS,
                               "CGST + SGST charged on an intra-state supply.",
                               expected="CGST + SGST", actual="CGST + SGST"))
        if invoice.cgst is not None and invoice.sgst is not None:
            if invoice.cgst == invoice.sgst:
                out.add(make_check("calc.cgst_sgst_equal", Cat.CALCULATION, "CGST equals SGST", St.PASS,
                                   f"CGST and SGST are both {fmt_inr(invoice.cgst)}."))
            else:
                out.add(make_check(
                    "calc.cgst_sgst_equal", Cat.CALCULATION, "CGST equals SGST", St.FAIL,
                    f"CGST {fmt_inr(invoice.cgst)} ≠ SGST {fmt_inr(invoice.sgst)}.",
                    expected="CGST = SGST", actual=f"{invoice.cgst} / {invoice.sgst}",
                    explanation="For intra-state supplies the GST rate is split equally between "
                                "CGST and SGST, so both amounts must be the same.",
                    suggestion="Recalculate both components as half of the total GST.",
                ))
    elif supply is SupplyType.INTER_STATE:
        if _nz(invoice.cgst) or _nz(invoice.sgst):
            out.add(make_check(
                "calc.tax_type", Cat.CALCULATION, "Correct tax type", St.FAIL,
                "CGST/SGST charged on an inter-state supply.", expected="IGST", actual="CGST + SGST",
                explanation="When the supplier and the place of supply are in different states, "
                            "IGST applies instead of CGST and SGST.",
                suggestion="Ask the supplier to re-issue the invoice with IGST.",
            ))
        elif not _nz(invoice.igst):
            out.add(make_check(
                "calc.tax_type", Cat.CALCULATION, "Correct tax type", St.MANUAL_REVIEW,
                "No IGST found on an inter-state supply.",
                explanation="Either the supply is exempt/nil-rated or the tax amount was not extracted.",
                suggestion="Confirm whether IGST was charged.",
            ))
        else:
            out.add(make_check("calc.tax_type", Cat.CALCULATION, "Correct tax type", St.PASS,
                               "IGST charged on an inter-state supply.", expected="IGST", actual="IGST"))


def _tax_amounts(invoice: InvoiceData, supply: SupplyType, reference: HSNReference,
                 tolerance: Decimal, out: RuleOutput) -> None:
    if not invoice.line_items:
        return
    expected_tax = ZERO
    for index, item in enumerate(invoice.line_items, start=1):
        rate, _ = applied_rate(item, invoice)
        if rate is None and item.hsn_sac:
            entry = reference.lookup(item.hsn_sac)
            if entry is not None:
                rate = entry.gst_rate
                out.assumptions.append(
                    f"Line {index} has no printed rate; the reference rate {rate}% was used to "
                    "estimate expected tax.")
        if rate is None or item.taxable_value is None:
            out.add(make_check(
                "calc.tax_amount", Cat.CALCULATION, "Tax amount", St.MANUAL_REVIEW,
                f"Expected tax cannot be computed: line {index} has no usable rate or value.",
                explanation="Tax is computed per line as taxable value × rate.",
                suggestion="Confirm the rate and taxable value of every line.",
            ))
            return
        expected_tax += item.taxable_value * rate / 100

    expected_tax = q2(expected_tax)
    if supply is SupplyType.INTRA_STATE:
        half = q2(expected_tax / 2)
        for head, value in (("cgst", invoice.cgst), ("sgst", invoice.sgst)):
            _compare(f"calc.{head}", f"{head.upper()} amount", half, value, tolerance, out,
                     f"{head.upper()} should be half of the total GST ({fmt_inr(expected_tax)}).")
    elif supply is SupplyType.INTER_STATE:
        _compare("calc.igst", "IGST amount", expected_tax, invoice.igst, tolerance, out,
                 "IGST should equal taxable value × GST rate.")

    charged = sum((t for t in (invoice.cgst, invoice.sgst, invoice.igst) if t is not None), ZERO)
    _compare("calc.total_tax", "Total tax amount", expected_tax, charged, tolerance, out,
             "Total GST should equal the sum of (line taxable value × line rate).")


def _compare(check_id: str, name: str, expected: Decimal, actual: Decimal | None, tolerance: Decimal,
             out: RuleOutput, rule: str) -> None:
    if actual is None:
        out.add(make_check(
            check_id, Cat.CALCULATION, name, St.MISSING, f"{name} not found on the invoice.",
            expected=expected,
            explanation=f"{rule} The amount could not be read from the invoice.",
            suggestion=f"Check that the {name} is printed and enter it if it is.",
        ))
        return
    diff = abs(expected - actual)
    if diff == ZERO:
        out.add(make_check(check_id, Cat.CALCULATION, name, St.PASS,
                           f"{name} {fmt_inr(actual)} is correct.", expected=expected, actual=actual))
    elif diff <= tolerance:
        out.add(make_check(check_id, Cat.CALCULATION, name, St.PASS,
                           f"{name} {fmt_inr(actual)} is within rounding tolerance of {fmt_inr(expected)} "
                           f"(difference {fmt_inr(diff)}).", expected=expected, actual=actual))
    else:
        out.add(make_check(
            check_id, Cat.CALCULATION, name, St.FAIL,
            f"{name} should be {fmt_inr(expected)} but the invoice shows {fmt_inr(actual)} "
            f"(difference {fmt_inr(diff)}).", expected=expected, actual=actual,
            explanation=rule,
            suggestion=f"Recalculate the {name} and request a corrected invoice or a "
                       "credit/debit note from the supplier.",
        ))


def _invoice_total(invoice: InvoiceData, tolerance: Decimal, out: RuleOutput) -> None:
    if invoice.total_invoice_value is None or invoice.taxable_value is None:
        return
    taxes = sum((t for t in (invoice.cgst, invoice.sgst, invoice.igst) if t is not None), ZERO)
    round_off = invoice.round_off or ZERO
    expected = q2(invoice.taxable_value + taxes + round_off)
    diff = abs(expected - invoice.total_invoice_value)

    if invoice.round_off is not None and abs(invoice.round_off) >= Decimal("1"):
        out.add(make_check(
            "calc.round_off", Cat.CALCULATION, "Round-off amount", St.WARNING,
            f"Round-off of {fmt_inr(invoice.round_off)} is unusually large.",
            explanation="Round-off normally rounds the total to the nearest rupee (under ₹1).",
            suggestion="Check that the round-off is not hiding a calculation difference.",
        ))

    formula = "taxable value + CGST + SGST + IGST" + (" + round-off" if invoice.round_off is not None else "")
    if diff == ZERO:
        out.add(make_check("calc.invoice_total", Cat.CALCULATION, "Invoice total", St.PASS,
                           f"Total {fmt_inr(invoice.total_invoice_value)} = {formula}.",
                           expected=expected, actual=invoice.total_invoice_value))
    elif diff <= tolerance:
        out.add(make_check(
            "calc.invoice_total", Cat.CALCULATION, "Invoice total", St.PASS,
            f"Total differs from {formula} ({fmt_inr(expected)}) by {fmt_inr(diff)}, within rounding "
            "tolerance.", severity=Severity.INFO, expected=expected, actual=invoice.total_invoice_value,
        ))
    else:
        out.add(make_check(
            "calc.invoice_total", Cat.CALCULATION, "Invoice total", St.FAIL,
            f"Total should be {fmt_inr(expected)} ({formula}) but the invoice shows "
            f"{fmt_inr(invoice.total_invoice_value)} (difference {fmt_inr(diff)}).",
            expected=expected, actual=invoice.total_invoice_value,
            explanation="The invoice total must equal the taxable value plus all GST charged "
                        "(plus any small round-off).",
            suggestion="Recalculate the total; ask the supplier for a corrected invoice if it is wrong.",
        ))

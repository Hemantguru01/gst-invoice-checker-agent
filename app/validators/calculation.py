"""GST arithmetic checks: line values, tax type, tax amounts and invoice total."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from app.models.invoice import InvoiceData, LineItem
from app.models.validation import CheckCategory as Cat
from app.models.validation import CheckStatus as St
from app.models.validation import Severity
from app.services.reference_data import HSNReference
from app.utils.money import fmt_inr, q2
from app.validators.base import RuleOutput, make_check
from app.validators.gstin import GSTINResult
from app.validators.hsn import SPECIAL_POS_CODES

ZERO = Decimal("0")


class SupplyType(str, Enum):
    INTRA_STATE = "intra-state"
    INTER_STATE = "inter-state"
    SPECIAL = "special (not modelled)"
    UNKNOWN = "unknown"



def _nz(value: Decimal | None) -> bool:
    return value is not None and value != ZERO


def determine_supply_type(invoice: InvoiceData, supplier: GSTINResult, state_names: dict[str, str],
                          out: RuleOutput, recipient: GSTINResult | None = None) -> SupplyType:
    supplier_state = supplier.state_code if supplier.state_name else None
    pos = invoice.place_of_supply_code

    special = list(invoice.supply_indicators)
    if pos in SPECIAL_POS_CODES:
        special.append(f"place of supply {pos} ({SPECIAL_POS_CODES[pos]})")
    if special:
        out.add(make_check(
            "supply.type", Cat.SUPPLY, "Supply type", St.MANUAL_REVIEW,
            f"The invoice indicates a special supply ({'; '.join(special)}). Exports, SEZ supplies, deemed "
            "exports and supplies under LUT/bond follow rules this tool does not model, so the tax type "
            "and amounts were not checked automatically.",
            explanation="Such supplies may be zero-rated (with or without IGST) or follow special "
                        "place-of-supply rules, so comparing supplier state with place of supply is not "
                        "enough to decide the tax.",
            suggestion="Have the zero-rating / SEZ / export treatment and the LUT or bond details "
                       "verified by a GST professional.",
        ))
        out.assumptions.append("Special-supply indicators were found; tax type and tax amounts were "
                               "not checked automatically.")
        return SupplyType.SPECIAL

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
            "rules for services) are only detected when the invoice text says so."
        )
        recipient_state = recipient.state_code if recipient is not None and recipient.state_name else None
        if recipient_state and recipient_state != pos:
            services = any((item.hsn_sac or "").startswith("99") for item in invoice.line_items)
            out.add(make_check(
                "supply.pos_rule", Cat.SUPPLY, "Place of supply vs recipient state", St.MANUAL_REVIEW,
                f"The recipient is registered in state {recipient_state} ({state_names.get(recipient_state)}) "
                f"but the place of supply is {pos} ({state_names.get(pos)}).",
                explanation=("For services, special place-of-supply rules (e.g. immovable property, events, "
                             "transport) may apply." if services else
                             "This is normal for bill-to/ship-to supplies of goods, but it can also be a "
                             "place-of-supply error.") + " The tool cannot confirm which rule applies.",
                suggestion="Confirm the place of supply under sections 10–13 of the IGST Act.",
            ))
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


@dataclass
class ExpectedAmounts:
    """One set of recomputed figures. Every tax and total check is measured against these,
    so the expected values and differences in the report always come from the same source."""

    taxable: Decimal | None = None
    taxable_basis: str = ""
    cgst: Decimal | None = None
    sgst: Decimal | None = None
    igst: Decimal | None = None
    tax: Decimal | None = None  # None when the expected GST cannot be computed


def check_calculations(invoice: InvoiceData, supplier: GSTINResult, reference: HSNReference,
                       state_names: dict[str, str], tolerance: Decimal,
                       recipient: GSTINResult | None = None) -> RuleOutput:
    out = RuleOutput()
    out.assumptions.append(
        f"Amounts within ₹{tolerance:.2f} of the expected value are treated as rounding differences."
    )

    _line_arithmetic(invoice, tolerance, out)
    supply = determine_supply_type(invoice, supplier, state_names, out, recipient)
    _tax_type(invoice, supply, out)
    expected = _expected_amounts(invoice, supply, reference, out)
    _tax_amounts(invoice, supply, expected, tolerance, out)
    _invoice_total(invoice, expected, tolerance, out)
    return out


def _line_arithmetic(invoice: InvoiceData, tolerance: Decimal, out: RuleOutput) -> None:
    for index, item in enumerate(invoice.line_items, start=1):
        if item.quantity is None or item.unit_price is None or item.taxable_value is None:
            continue
        expected = q2(item.quantity * item.unit_price - (item.discount or ZERO))
        formula = f"{item.quantity} × {fmt_inr(item.unit_price)}"
        if item.discount is not None:
            formula += f" − discount {fmt_inr(item.discount)}"
        diff = abs(expected - item.taxable_value)
        if diff <= tolerance:
            out.add(make_check(
                f"calc.line{index}.value", Cat.CALCULATION, f"Line {index} quantity × price", St.PASS,
                f"{formula} = {fmt_inr(expected)}.",
                expected=expected, actual=item.taxable_value,
            ))
        else:
            out.add(make_check(
                f"calc.line{index}.value", Cat.CALCULATION, f"Line {index} quantity × price", St.FAIL,
                f"Line {index}: {formula} = {fmt_inr(expected)}, "
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
    basis = "Sum of line values"
    if invoice.discount:
        line_sum = q2(line_sum - invoice.discount)
        basis = f"Sum of line values less the invoice discount of {fmt_inr(invoice.discount)}"
    diff = abs(line_sum - invoice.taxable_value)
    if diff <= tolerance:
        out.add(make_check(
            "calc.taxable_total", Cat.CALCULATION, "Taxable value = sum of lines", St.PASS,
            f"{basis} ({fmt_inr(line_sum)}) matches the taxable value.",
            expected=line_sum, actual=invoice.taxable_value,
        ))
    else:
        out.add(make_check(
            "calc.taxable_total", Cat.CALCULATION, "Taxable value = sum of lines", St.FAIL,
            f"{basis} is {fmt_inr(line_sum)} but taxable value shows "
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
                    expected="CGST = SGST", actual=f"{fmt_inr(invoice.cgst)} / {fmt_inr(invoice.sgst)}",
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


def _line_rate(index: int, item: LineItem, invoice: InvoiceData, reference: HSNReference,
               out: RuleOutput) -> Decimal | None:
    """Rate used to recompute a line's tax. Never derived from the invoice's own tax amounts
    (that would make the tax check circular) and never taken from an unverified reference entry."""
    if item.gst_rate is not None:
        return item.gst_rate
    entry = reference.lookup(item.hsn_sac) if item.hsn_sac else None
    if entry is None or entry.confidence != "high":
        return None
    if invoice.invoice_date is not None and invoice.invoice_date < (entry.effective_from or reference.effective_from):
        return None
    out.assumptions.append(
        f"Line {index} has no printed rate; the local reference rate {entry.gst_rate}% for "
        f"{entry.code} (not legally authoritative) was used to estimate its tax.")
    return entry.gst_rate


def _expected_amounts(invoice: InvoiceData, supply: SupplyType, reference: HSNReference,
                      out: RuleOutput) -> ExpectedAmounts:
    expected = ExpectedAmounts()
    line_values = [item.taxable_value for item in invoice.line_items]
    discount = invoice.discount or ZERO
    if line_values and all(v is not None for v in line_values):
        line_sum = q2(sum(line_values, ZERO))  # type: ignore[arg-type]
        expected.taxable = q2(line_sum - discount)
        expected.taxable_basis = (f"line taxable values {fmt_inr(line_sum)} − invoice discount {fmt_inr(discount)} ="
                                  if invoice.discount else "sum of line taxable values")
        out.assumptions.append(
            "Expected GST and the expected invoice total are computed from the line taxable values "
            "and rates as printed. Quantity × price errors are reported separately and are not "
            "corrected in these figures.")
    elif invoice.taxable_value is not None:
        expected.taxable = invoice.taxable_value
        expected.taxable_basis = "printed taxable value"
    if not invoice.line_items or supply is SupplyType.SPECIAL:
        return expected

    rates: list[Decimal] = []
    for index, item in enumerate(invoice.line_items, start=1):
        rate = _line_rate(index, item, invoice, reference, out)
        if rate is None or item.taxable_value is None:
            out.add(make_check(
                "calc.tax_amount", Cat.CALCULATION, "Tax amount", St.MANUAL_REVIEW,
                f"Expected tax cannot be computed: line {index} has no printed GST rate (or no taxable "
                "value), and no verified reference rate is available.",
                explanation="Tax is computed per line as taxable value × rate. The tool does not work "
                            "the rate back from the tax charged, because that would always agree with "
                            "itself, and it does not guess rates.",
                suggestion="Confirm the rate and taxable value of every line.",
            ))
            return expected
        rates.append(rate)

    if invoice.discount and len(set(rates)) > 1:
        out.add(make_check(
            "calc.tax_amount", Cat.CALCULATION, "Tax amount", St.MANUAL_REVIEW,
            f"An invoice-level discount of {fmt_inr(invoice.discount)} applies to lines with different GST "
            "rates; how it is split between the rates is not shown, so expected tax cannot be computed.",
            explanation="A discount shown on the invoice reduces the taxable value, but with several "
                        "rates the tax depends on which lines the discount belongs to.",
            suggestion="Confirm how the discount is allocated to each line.",
        ))
        return expected

    raw_tax = sum((item.taxable_value * rate / 100  # type: ignore[operator]
                   for item, rate in zip(invoice.line_items, rates)), ZERO)
    if invoice.discount:
        raw_tax -= invoice.discount * rates[0] / 100
        out.assumptions.append(
            f"The invoice-level discount of {fmt_inr(invoice.discount)} is treated as reducing the taxable "
            "value (discount shown on the invoice at the time of supply).")

    if supply is SupplyType.INTRA_STATE:
        expected.cgst = expected.sgst = q2(raw_tax / 2)
        expected.tax = expected.cgst + expected.sgst
    elif supply is SupplyType.INTER_STATE:
        expected.igst = expected.tax = q2(raw_tax)
    else:
        expected.tax = q2(raw_tax)
    return expected


def _tax_amounts(invoice: InvoiceData, supply: SupplyType, expected: ExpectedAmounts,
                 tolerance: Decimal, out: RuleOutput) -> None:
    if expected.tax is None:
        return
    if supply is SupplyType.INTRA_STATE:
        for head, should_be, value in (("cgst", expected.cgst, invoice.cgst), ("sgst", expected.sgst, invoice.sgst)):
            _compare(f"calc.{head}", f"{head.upper()} amount", should_be, value, tolerance, out,  # type: ignore[arg-type]
                     f"{head.upper()} should be half of the total GST ({fmt_inr(expected.tax)}).")
    elif supply is SupplyType.INTER_STATE:
        _compare("calc.igst", "IGST amount", expected.tax, invoice.igst, tolerance, out,
                 "IGST should equal taxable value × GST rate.")

    charged = sum((t for t in (invoice.cgst, invoice.sgst, invoice.igst) if t is not None), ZERO)
    _compare("calc.total_tax", "Total tax amount", expected.tax, charged, tolerance, out,
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


def _total_check(check_id: str, name: str, invoice: InvoiceData, expected: Decimal, formula: str,
                 tolerance: Decimal, explanation: str, out: RuleOutput) -> None:
    printed = invoice.total_invoice_value
    assert printed is not None
    diff = abs(expected - printed)
    if diff == ZERO:
        out.add(make_check(check_id, Cat.CALCULATION, name, St.PASS,
                           f"Printed total {fmt_inr(printed)} = {formula}.", expected=expected, actual=printed))
    elif diff <= tolerance:
        out.add(make_check(
            check_id, Cat.CALCULATION, name, St.PASS,
            f"Printed total {fmt_inr(printed)} differs from {fmt_inr(expected)} ({formula}) by {fmt_inr(diff)}, "
            "within rounding tolerance.", severity=Severity.INFO, expected=expected, actual=printed,
        ))
    else:
        out.add(make_check(
            check_id, Cat.CALCULATION, name, St.FAIL,
            f"Printed total {fmt_inr(printed)} does not match {fmt_inr(expected)} ({formula}); "
            f"difference {fmt_inr(diff)}.", expected=expected, actual=printed,
            explanation=explanation,
            suggestion="Recalculate the total; ask the supplier for a corrected invoice if it is wrong.",
        ))


def _invoice_total(invoice: InvoiceData, amounts: ExpectedAmounts, tolerance: Decimal, out: RuleOutput) -> None:
    """Two separate checks, each from a single, stated source:
    1. printed total vs the printed components (is the invoice internally consistent?)
    2. printed total vs the expected total built from independently recomputed GST."""
    if invoice.total_invoice_value is None:
        return
    round_off = invoice.round_off or ZERO
    round_note = f" + round-off {fmt_inr(invoice.round_off)}" if invoice.round_off is not None else ""

    if invoice.round_off is not None and abs(invoice.round_off) >= Decimal("1"):
        out.add(make_check(
            "calc.round_off", Cat.CALCULATION, "Round-off amount", St.WARNING,
            f"Round-off of {fmt_inr(invoice.round_off)} is unusually large.",
            explanation="Round-off normally rounds the total to the nearest rupee (under ₹1).",
            suggestion="Check that the round-off is not hiding a calculation difference.",
        ))

    if invoice.taxable_value is not None:
        printed_tax = sum((t for t in (invoice.cgst, invoice.sgst, invoice.igst) if t is not None), ZERO)
        heads = " + ".join(f"{h.upper()} {fmt_inr(getattr(invoice, h))}" for h in ("cgst", "sgst", "igst")
                           if getattr(invoice, h) is not None) or "no GST printed"
        _total_check(
            "calc.invoice_total", "Invoice total vs printed components", invoice,
            q2(invoice.taxable_value + printed_tax + round_off),
            f"printed taxable value {fmt_inr(invoice.taxable_value)} + printed {heads}{round_note}",
            tolerance, "The printed total must equal the printed taxable value plus the GST printed on "
                       "the invoice (plus any small round-off). A mismatch means the invoice does not add up "
                       "on its own figures.", out)

    if amounts.taxable is None or amounts.tax is None:
        out.add(make_check(
            "calc.invoice_total_expected", Cat.CALCULATION, "Invoice total vs expected GST", St.MANUAL_REVIEW,
            "The expected invoice total cannot be computed because the expected GST could not be "
            "recomputed independently.",
            explanation="The expected total is the taxable value plus GST recomputed from line values and "
                        "rates. Without it, only the printed figures can be compared with each other.",
            suggestion="Confirm the GST rate and taxable value of every line.",
        ))
        return
    _total_check(
        "calc.invoice_total_expected", "Invoice total vs expected GST", invoice,
        q2(amounts.taxable + amounts.tax + round_off),
        f"{amounts.taxable_basis} {fmt_inr(amounts.taxable)} + expected GST {fmt_inr(amounts.tax)}{round_note}",
        tolerance, "The invoice total should equal the taxable value plus the GST that is actually due. "
                   "The expected GST here is the same recomputed figure used by the CGST/SGST/IGST and "
                   "total-tax checks.", out)

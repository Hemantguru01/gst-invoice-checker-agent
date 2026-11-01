"""Deterministic rule engine: runs every validator and derives the overall status."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.models.invoice import ExtractionResult, ExtractionStatus, FieldSource
from app.models.validation import (
    CheckCategory,
    CheckStatus,
    OverallStatus,
    Severity,
    ValidationCheck,
    ValidationSummary,
)
from app.services.reference_data import HSNReference, StateCodes
from app.services.storage import DuplicateCandidate
from app.validators.base import RuleOutput, make_check
from app.validators.calculation import check_calculations
from app.validators.fields import check_fields
from app.validators.gstin_rules import check_gstins
from app.validators.hsn import check_hsn


@dataclass
class RuleEngineResult:
    checks: list[ValidationCheck]
    assumptions: list[str] = field(default_factory=list)
    overall_status: OverallStatus = OverallStatus.NEEDS_REVIEW
    summary: ValidationSummary | None = None


class RuleEngine:
    def __init__(self, hsn_reference: HSNReference, state_codes: StateCodes,
                 rounding_tolerance: float = 1.0) -> None:
        self.hsn_reference = hsn_reference
        self.state_codes = state_codes
        self.tolerance = Decimal(str(rounding_tolerance))

    def run(self, extraction: ExtractionResult, today: date | None = None,
            duplicates: list[DuplicateCandidate] | None = None) -> RuleEngineResult:
        """`duplicates` is None when no history lookup was made (then no duplicate check is reported)."""
        out = RuleOutput()
        if extraction.status is ExtractionStatus.FAILED:
            out.add(make_check(
                "extraction.status", CheckCategory.FIELDS, "Invoice text extraction", CheckStatus.FAIL,
                f"Text could not be extracted from the document: {extraction.error or 'unknown error'}",
                explanation="Without readable text, none of the GST checks can run.",
                suggestion="Upload a clearer scan (at least 200 DPI, straight, well lit) or a "
                           "text-based PDF, or enter the invoice details manually.",
            ))

        invoice = extraction.invoice
        out.extend(check_fields(extraction, today))
        if duplicates is not None:
            out.add(_duplicate_check(extraction, duplicates))
        ocr_fields = {f for f, src in extraction.field_sources.items() if src is FieldSource.OCR}
        gstin_out, supplier, recipient = check_gstins(invoice, self.state_codes.codes, ocr_fields)
        out.extend(gstin_out)
        if invoice.place_of_supply and not invoice.place_of_supply_code:
            out.add(make_check(
                "supply.pos_code", CheckCategory.SUPPLY, "Place of supply state", CheckStatus.WARNING,
                f"Place of supply '{invoice.place_of_supply}' does not match a known state or UT.",
                field="place_of_supply", actual=invoice.place_of_supply,
                explanation="The place of supply must name an Indian state/UT so the tax type can be decided.",
                suggestion="Write the place of supply as '<state code>-<state name>', e.g. '29-Karnataka'.",
            ))
        out.extend(check_hsn(invoice, self.hsn_reference))
        out.extend(check_calculations(invoice, supplier, self.hsn_reference, self.state_codes.codes,
                                      self.tolerance, recipient))

        checks = _dedupe(out.checks)
        return RuleEngineResult(
            checks=checks,
            assumptions=out.assumptions,
            overall_status=overall_status(checks),
            summary=summarize(checks),
        )


_STATUS_RANK = {CheckStatus.PASS: 0, CheckStatus.WARNING: 1, CheckStatus.MANUAL_REVIEW: 2,
                CheckStatus.MISSING: 3, CheckStatus.FAIL: 4}
_SEVERITY_RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.ERROR: 2}


def _duplicate_check(extraction: ExtractionResult, duplicates: list[DuplicateCandidate]) -> ValidationCheck:
    inv = extraction.invoice
    name = "Duplicate invoice number"
    if not duplicates:
        return make_check("fields.invoice_number.duplicate", CheckCategory.FIELDS, name, CheckStatus.PASS,
                          f"No other processed invoice from this supplier uses invoice number "
                          f"'{inv.invoice_number}' in the same financial year.", field="invoice_number")
    printed = inv.invoice_number_raw or inv.invoice_number
    matches = []
    for d in duplicates:
        how = "identical" if d.invoice_number_raw == printed else f"printed as '{d.invoice_number_raw}'"
        matches.append(f"'{d.original_filename}' uploaded {d.uploaded_at:%Y-%m-%d %H:%M} UTC ({how})")
    return make_check(
        "fields.invoice_number.duplicate", CheckCategory.FIELDS, name, CheckStatus.WARNING,
        f"Invoice number '{printed}' from this supplier was already processed: {'; '.join(matches)}. "
        f"Numbers were compared as '{inv.invoice_number_key}' (case and spacing ignored; '/' and '-' kept).",
        field="invoice_number", actual=printed,
        explanation="A supplier must use a unique invoice number within a financial year (CGST Rule 46). "
                    "The same number twice can mean a re-upload, a duplicate claim, or two different invoices "
                    "whose numbers differ only in spacing or letter case. The records are not merged.",
        suggestion="Compare both invoices. If they are the same document, avoid claiming input tax credit twice; "
                   "if they differ, ask the supplier why the number was reused.",
    )


def _dedupe(checks: list[ValidationCheck]) -> list[ValidationCheck]:
    """Keep one result per check ID: the most severe, so a repeat can never hide a problem."""
    seen: dict[str, int] = {}
    result: list[ValidationCheck] = []
    for check in checks:
        if check.check_id not in seen:
            seen[check.check_id] = len(result)
            result.append(check)
            continue
        kept = result[seen[check.check_id]]
        if (_SEVERITY_RANK[check.severity], _STATUS_RANK[check.status]) > \
                (_SEVERITY_RANK[kept.severity], _STATUS_RANK[kept.status]):
            result[seen[check.check_id]] = check
    return result


def overall_status(checks: list[ValidationCheck]) -> OverallStatus:
    if any(c.severity is Severity.ERROR for c in checks):
        return OverallStatus.INVALID
    if any(c.status is not CheckStatus.PASS for c in checks):
        return OverallStatus.NEEDS_REVIEW
    return OverallStatus.VALID


def summarize(checks: list[ValidationCheck]) -> ValidationSummary:
    return ValidationSummary(
        total_checks=len(checks),
        passed=sum(c.status is CheckStatus.PASS for c in checks),
        errors=sum(c.severity is Severity.ERROR for c in checks),
        warnings=sum(c.status in (CheckStatus.WARNING, CheckStatus.MISSING)
                     and c.severity is not Severity.ERROR for c in checks),
        manual_review=sum(c.status is CheckStatus.MANUAL_REVIEW for c in checks),
    )

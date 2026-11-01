"""Shared helpers for building validation checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.models.validation import CheckCategory, CheckStatus, Severity, ValidationCheck

_DEFAULT_SEVERITY = {
    CheckStatus.PASS: Severity.INFO,
    CheckStatus.FAIL: Severity.ERROR,
    CheckStatus.MISSING: Severity.ERROR,
    CheckStatus.WARNING: Severity.WARNING,
    CheckStatus.MANUAL_REVIEW: Severity.WARNING,
}


def make_check(
    check_id: str,
    category: CheckCategory,
    name: str,
    status: CheckStatus,
    message: str,
    *,
    severity: Severity | None = None,
    field: str | None = None,
    expected: object | None = None,
    actual: object | None = None,
    explanation: str | None = None,
    suggestion: str | None = None,
) -> ValidationCheck:
    return ValidationCheck(
        check_id=check_id,
        category=category,
        name=name,
        status=status,
        severity=severity or _DEFAULT_SEVERITY[status],
        message=message,
        field=field,
        expected=_fmt(expected),
        actual=_fmt(actual),
        explanation=explanation,
        suggestion=suggestion,
    )


def _fmt(value: object | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return f"{value:,.2f}"
    return str(value)


@dataclass
class RuleOutput:
    checks: list[ValidationCheck] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)

    def add(self, check: ValidationCheck) -> None:
        self.checks.append(check)

    def extend(self, other: "RuleOutput") -> None:
        self.checks.extend(other.checks)
        self.assumptions.extend(a for a in other.assumptions if a not in self.assumptions)

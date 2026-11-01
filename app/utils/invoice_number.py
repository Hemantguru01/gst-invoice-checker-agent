"""Invoice number normalisation.

Three forms are kept apart:
  * raw     - exactly as extracted (or typed); kept for display and audit, never modified.
  * display - whitespace tidied only: runs collapsed, and spaces around '/' and '-' removed
              ("INV / 2026 / 001" -> "INV/2026/001"). No other character is ever changed.
  * key     - comparison value for duplicate detection: display form in upper case, dash variants
              as '-', inner spaces removed ("GST 001" -> "GST001"). Separators are kept, so
              INV/2026/001, INV-2026-001 and INV2026001 stay three different invoices.
"""

from __future__ import annotations

import re

# Hyphen and dash variants that PDFs and OCR produce for the same printed separator.
_DASHES = "‐‑‒–—−"
_SEPARATOR = rf"[/\-{_DASHES}]"
_AROUND_SEPARATOR = re.compile(rf"\s*({_SEPARATOR})\s*")
_RULE46_CHARS = re.compile(r"^[A-Z0-9/\-]+$")
RULE46_MAX_LENGTH = 16


def normalize_invoice_number(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = _AROUND_SEPARATOR.sub(r"\1", raw.strip())
    value = re.sub(r"\s+", " ", value)
    return value or None


def invoice_number_key(value: str | None) -> str | None:
    display = normalize_invoice_number(value)
    if display is None:
        return None
    key = display.upper()
    for dash in _DASHES:
        key = key.replace(dash, "-")
    return key.replace(" ", "") or None


def rule46_problem(value: str | None) -> str | None:
    """Why the number breaks CGST Rule 46 (max 16 characters: letters, digits, '-' and '/'), or None.

    Checked on the comparison key, so cosmetic spaces printed inside the number are tolerated."""
    key = invoice_number_key(value)
    if key is None:
        return None
    if len(key) > RULE46_MAX_LENGTH:
        return f"it is {len(key)} characters long (the limit is {RULE46_MAX_LENGTH}, ignoring spaces)"
    if not _RULE46_CHARS.match(key):
        bad = sorted({c for c in key if not re.match(r"[A-Z0-9/\-]", c)})
        return f"it contains characters other than letters, digits, '-' and '/' ({' '.join(bad)})"
    return None

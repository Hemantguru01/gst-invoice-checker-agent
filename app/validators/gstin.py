"""GSTIN validation: presence, format, state code and checksum.

GSTIN layout (15 characters):
    1-2   state code (numeric)
    3-12  PAN of the taxpayer (5 letters, 4 digits, 1 letter)
    13    entity number for the same PAN in the state (1-9, A-Z)
    14    'Z' by default
    15    checksum character (base-36 Luhn mod N)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_FORMAT_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z][0-9A-Z][0-9A-Z]$")


class GSTINError(str, Enum):
    MISSING = "missing"
    LENGTH = "length"
    FORMAT = "format"
    STATE_CODE = "state_code"
    CHECKSUM = "checksum"


@dataclass
class GSTINResult:
    gstin: str | None
    is_valid: bool
    error: GSTINError | None = None
    message: str = ""
    state_code: str | None = None
    state_name: str | None = None
    pan: str | None = None
    expected_checksum: str | None = None
    non_default_z: bool = False


def normalize_gstin(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = re.sub(r"[\s\-]", "", value).upper()
    return cleaned or None


def compute_checksum(first_14: str) -> str:
    """Checksum character for the first 14 characters of a GSTIN."""
    total = 0
    for index, char in enumerate(first_14):
        value = CHARSET.index(char)
        product = value * (2 if index % 2 else 1)
        total += product // 36 + product % 36
    return CHARSET[(36 - total % 36) % 36]


def validate_gstin(value: str | None, state_codes: dict[str, str]) -> GSTINResult:
    gstin = normalize_gstin(value)
    if not gstin:
        return GSTINResult(None, False, GSTINError.MISSING, "GSTIN is missing.")
    if len(gstin) != 15:
        return GSTINResult(
            gstin, False, GSTINError.LENGTH, f"GSTIN must be 15 characters; found {len(gstin)}."
        )
    if not _FORMAT_RE.match(gstin):
        return GSTINResult(gstin, False, GSTINError.FORMAT, _describe_format_problem(gstin))

    state_code = gstin[:2]
    state_name = state_codes.get(state_code)
    if state_name is None:
        return GSTINResult(
            gstin,
            False,
            GSTINError.STATE_CODE,
            f"State code '{state_code}' is not a recognised GST state/UT code.",
            state_code=state_code,
            pan=gstin[2:12],
        )

    expected = compute_checksum(gstin[:14])
    if expected != gstin[14]:
        return GSTINResult(
            gstin,
            False,
            GSTINError.CHECKSUM,
            f"Checksum mismatch: the last character is '{gstin[14]}' but the first 14 "
            f"characters produce '{expected}'. The format is correct, so this usually means "
            "a typing or OCR error in one of the characters.",
            state_code=state_code,
            state_name=state_name,
            pan=gstin[2:12],
            expected_checksum=expected,
        )

    return GSTINResult(
        gstin,
        True,
        message=f"Valid format, state code ({state_code} - {state_name}) and checksum.",
        state_code=state_code,
        state_name=state_name,
        pan=gstin[2:12],
        non_default_z=gstin[13] != "Z",
    )


def _describe_format_problem(gstin: str) -> str:
    if not gstin.isalnum():
        return "GSTIN contains characters other than letters and digits."
    if not gstin[:2].isdigit():
        return "First two characters (state code) must be digits."
    pan = gstin[2:12]
    if not re.fullmatch(r"[A-Z]{5}[0-9]{4}[A-Z]", pan):
        return f"Characters 3-12 ('{pan}') do not follow the PAN pattern AAAAA9999A."
    if gstin[12] == "0":
        return "13th character (entity number) cannot be 0."
    return "GSTIN does not match the standard 15-character pattern."

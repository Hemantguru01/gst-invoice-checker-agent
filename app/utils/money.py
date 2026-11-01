"""Decimal helpers for Indian currency amounts."""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

_CURRENCY_TOKENS = re.compile(r"(₹|rs\.?|inr|/-)", re.IGNORECASE)
TWO_PLACES = Decimal("0.01")


def parse_amount(value: object) -> Decimal | None:
    """Parse '1,06,200.00', '₹ 500', 'Rs. 1,200/-' etc. into a Decimal.

    Returns None when the value is empty or not a number; never guesses.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = _CURRENCY_TOKENS.sub("", str(value)).replace(",", "").replace(" ", "").strip()
    if not text:
        return None
    if not re.fullmatch(r"-?\d+(\.\d+)?", text):
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def q2(value: Decimal) -> Decimal:
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def fmt_inr(value: Decimal | None) -> str:
    """Format with Indian digit grouping, e.g. ₹1,77,000.00."""
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    whole, frac = f"{abs(q2(value)):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{sign}₹{whole}.{frac}"

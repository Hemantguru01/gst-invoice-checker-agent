"""Rule-based parser that turns invoice text lines into structured InvoiceData.

The parser only records values it can locate next to a recognisable label or in the
item table. Anything it cannot find stays None and is reported as missing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache

from app.models.invoice import InvoiceData, LineItem
from app.services.ocr_service import TextLine
from app.services.reference_data import StateCodes
from app.utils.money import parse_amount

LOW_CONFIDENCE = 0.85

_GSTIN_LABEL = re.compile(r"\bGST(?:IN)?(?:\s*/\s*UIN)?(?:\s*(?:No\.?|Number))?\s*[:\-]?\s*([A-Z0-9]{10,20})\b", re.I)
_INVOICE_NO = re.compile(r"\b(?:Invoice|Inv|Bill)\s*(?:No\.?|Number|#)\s*[:\-]?\s*([A-Za-z0-9/\-]+)", re.I)
_DATE_LABEL = re.compile(r"\b(?:Invoice\s*|Inv\.?\s*|Bill\s*)?Date(?:\s*of\s*Invoice)?\s*[:\-]?\s*"
                         r"(\d{1,4}[\-/.]\d{1,2}[\-/.]\d{2,4}|\d{1,2}[\s\-/][A-Za-z]{3,9}[\s\-/,]+\d{4})", re.I)
_POS = re.compile(r"\bPlace\s*of\s*Supply\s*[:\-]?\s*(.+)$", re.I)
_NUMBER_TOKEN = re.compile(r"-?\d[\d,]*(?:\.\d+)?\s*%?")

_SECTION_RECIPIENT = re.compile(r"\b(bill(?:ed)?\s*to|buyer|recipient|customer|ship(?:ped)?\s*to|consignee)\b", re.I)
_TABLE_HEADER = re.compile(r"\b(HSN|SAC)\b", re.I)

_AMOUNT_LABELS: dict[str, tuple[str, ...]] = {
    "total_invoice_value": ("total invoice value", "grand total", "invoice total", "total amount payable",
                            "amount payable", "net amount", "total amount", "invoice value"),
    "taxable_value": ("total taxable value", "taxable value", "taxable amount", "sub total", "subtotal"),
    "cgst": ("cgst",),
    "sgst": ("sgst", "utgst"),
    "igst": ("igst",),
    "round_off": ("round off", "round-off", "rounding"),
}
_AMOUNT_EXCLUDE = {
    "total_invoice_value": ("tax", "words"),
    "taxable_value": (),
    "cgst": ("total", "rate"),
    "sgst": ("total", "rate"),
    "igst": ("total", "rate"),
    "round_off": (),
}
_SUMMARY_START = re.compile(r"\b(taxable value|sub\s*total|cgst|sgst|igst|total)\b", re.I)


@dataclass
class ParseResult:
    invoice: InvoiceData
    uncertain_fields: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)


def parse_date(raw: str) -> date | None:
    """Parse common Indian invoice date formats (day first). Returns None if ambiguous/unknown."""
    text = re.sub(r"\s+", " ", raw.strip().rstrip(",."))
    formats = ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y-%m-%d", "%d-%m-%y", "%d/%m/%y",
               "%d-%b-%Y", "%d %b %Y", "%d-%B-%Y", "%d %B %Y", "%d/%b/%Y", "%d %b, %Y", "%d %B, %Y")
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _numbers(text: str) -> list[tuple[Decimal, bool]]:
    """All numbers in text as (value, is_percentage)."""
    found = []
    for match in _NUMBER_TOKEN.finditer(text):
        token = match.group(0).strip()
        is_pct = token.endswith("%")
        value = parse_amount(token.rstrip("%").strip())
        if value is not None:
            found.append((value, is_pct))
    return found


def _last_amount(text: str) -> Decimal | None:
    amounts = [v for v, pct in _numbers(text) if not pct]
    return amounts[-1] if amounts else None


@lru_cache
def _label_regex(label: str) -> re.Pattern[str]:
    """'taxable value' -> matches 'taxable value', 'taxablevalue', 'taxable  value'."""
    return re.compile(r"\s*".join(re.escape(word) for word in label.split()))


def _after_label(text: str, label_pattern: str) -> str | None:
    match = re.search(label_pattern + r"\s*[:\-]\s*(.+)$", text, re.I)
    if not match:
        return None
    value = re.split(r"\s{2,}", match.group(1).strip())[0].strip()
    return value or None


class InvoiceParser:
    def __init__(self, state_codes: StateCodes) -> None:
        self.state_codes = state_codes

    def parse(self, lines: list[TextLine]) -> ParseResult:
        inv = InvoiceData()
        result = ParseResult(inv)
        section = "supplier"
        table_started = False
        table_done = False
        pending_name_for: str | None = None

        for line in lines:
            text = line.text.strip()
            low_conf = line.confidence < LOW_CONFIDENCE
            if not text:
                continue

            def mark(field_name: str) -> None:
                if low_conf:
                    result.uncertain_fields.add(field_name)

            # Name on the line after a "Bill To:" style header.
            if pending_name_for and not getattr(inv, pending_name_for) and not _GSTIN_LABEL.search(text):
                setattr(inv, pending_name_for, re.split(r"\s{2,}", text)[0].strip())
                mark(pending_name_for)
                pending_name_for = None
                continue
            pending_name_for = None

            if _TABLE_HEADER.search(text) and re.search(r"\b(qty|quantity|rate|amount|value|description)\b", text, re.I):
                table_started, section = True, "items"
                continue

            if table_started and not table_done:
                if _SUMMARY_START.search(text) and not re.search(r"\b\d{4,8}\b\s+\d", text):
                    table_done = True
                else:
                    item = self._parse_item_row(text)
                    if item is not None:
                        inv.line_items.append(item)
                        mark("line_items")
                    continue

            # Section switches (supplier / recipient blocks).
            if _SECTION_RECIPIENT.search(text) and section != "items":
                section = "recipient"
                name = _after_label(text, r"(?:bill(?:ed)?\s*to|buyer|recipient(?:\s*name)?|customer(?:\s*name)?|ship(?:ped)?\s*to|consignee)")
                if name and not _GSTIN_LABEL.search(name):
                    if not inv.recipient_name:
                        inv.recipient_name = name
                        mark("recipient_name")
                elif not re.search(r"gstin|state|address", text, re.I):
                    pending_name_for = "recipient_name"
            elif re.match(r"^\s*(supplier|seller|sold\s*by)\b", text, re.I) and section != "items":
                section = "supplier"
                name = _after_label(text, r"(?:supplier|seller|sold\s*by)(?:\s*name)?")
                if name and not _GSTIN_LABEL.search(name) and not inv.supplier_name:
                    inv.supplier_name = name
                    mark("supplier_name")
                elif not name and not re.search(r"gstin", text, re.I):
                    pending_name_for = "supplier_name"

            self._parse_labels(text, section, inv, mark)

        if inv.supplier_name is None:
            guess = self._guess_supplier_name(lines)
            if guess:
                inv.supplier_name = guess
                result.uncertain_fields.add("supplier_name")
                result.warnings.append("Supplier name was inferred from the document header.")
        self._finalise_place_of_supply(inv, result)
        return result

    def _parse_labels(self, text: str, section: str, inv: InvoiceData, mark) -> None:
        for match in _GSTIN_LABEL.finditer(text):
            value = match.group(1).upper()
            prefix = text[: match.start()].lower()
            if "recipient" in prefix or "buyer" in prefix or "customer" in prefix or "bill" in prefix:
                target = "recipient_gstin"
            elif "supplier" in prefix or "seller" in prefix:
                target = "supplier_gstin"
            else:
                target = "recipient_gstin" if section == "recipient" else "supplier_gstin"
            if getattr(inv, target) is None:
                setattr(inv, target, value)
                mark(target)

        if inv.invoice_number is None:
            m = _INVOICE_NO.search(text)
            if m:
                inv.invoice_number = m.group(1)
                mark("invoice_number")

        if inv.invoice_date is None and inv.invoice_date_raw is None:
            m = _DATE_LABEL.search(text)
            if m and not re.search(r"\b(due|order|po|delivery)\s*date", text, re.I):
                inv.invoice_date_raw = m.group(1)
                inv.invoice_date = parse_date(m.group(1))
                mark("invoice_date")

        if inv.place_of_supply is None:
            m = _POS.search(text)
            if m:
                inv.place_of_supply = re.split(r"\s{2,}", m.group(1).strip())[0]
                mark("place_of_supply")

        if not inv.supplier_name:
            name = _after_label(text, r"^\s*(?:supplier|seller)\s*name")
            if name:
                inv.supplier_name = name
                mark("supplier_name")
        if not inv.recipient_name:
            name = _after_label(text, r"^\s*(?:recipient|buyer|customer)\s*name")
            if name:
                inv.recipient_name = name
                mark("recipient_name")

        lowered = text.lower()
        for field_name, labels in _AMOUNT_LABELS.items():
            if getattr(inv, field_name) is not None:
                continue
            match = next((m for lb in labels if (m := _label_regex(lb).search(lowered))), None)
            if match is None:
                continue
            start, end = match.span()
            if any(ex in lowered[:start] + lowered[end:end + 8] for ex in _AMOUNT_EXCLUDE[field_name]):
                continue
            if field_name in ("cgst", "sgst", "igst") and re.search(r"\b(cgst|sgst|igst|utgst)\b.*\b(cgst|sgst|igst|utgst)\b", lowered):
                continue  # header row naming several tax heads
            amount = _last_amount(text[end:])
            if amount is not None:
                setattr(inv, field_name, amount)
                mark(field_name)

    def _parse_item_row(self, text: str) -> LineItem | None:
        cells = [c.strip() for c in re.split(r"\s{2,}|\s*\|\s*", text) if c.strip()]
        hsn_index = next((i for i, c in enumerate(cells) if re.fullmatch(r"\d{4,8}", c) and i > 0), None)
        if hsn_index is None:
            # OCR may merge cells with single spaces; fall back to a line regex.
            m = re.match(r"^(?:\d{1,3}[.)]?\s+)?(?P<desc>.*?[A-Za-z].*?)\s+(?P<hsn>\d{4,8})\s+(?P<rest>.+)$", text)
            if m:
                desc, hsn, rest_cells = m.group("desc"), m.group("hsn"), m.group("rest").split()
            else:
                # Row without any HSN/SAC code: keep it (it is reported as missing a code).
                m = re.match(r"^(?:\d{1,3}[.)]?\s+)?(?P<desc>[^\d|]*[A-Za-z][^|]*?)\s{2,}(?P<rest>[\d,.%\sA-Za-z|]+)$", text)
                if not m or len([n for n, _ in _numbers(m.group("rest"))]) < 2:
                    return None
                desc, hsn, rest_cells = m.group("desc"), None, m.group("rest").split()
        else:
            desc_cells = cells[:hsn_index]
            if desc_cells and re.fullmatch(r"\d{1,3}[.)]?", desc_cells[0]):
                desc_cells = desc_cells[1:]
            desc, hsn, rest_cells = " ".join(desc_cells), cells[hsn_index], cells[hsn_index + 1:]

        item = LineItem(description=desc.strip() or None, hsn_sac=hsn)
        numbers: list[Decimal] = []
        for cell in rest_cells:
            for part in cell.split():
                if part.endswith("%"):
                    item.gst_rate = parse_amount(part.rstrip("%"))
                    continue
                value = parse_amount(part)
                if value is not None:
                    numbers.append(value)
                elif re.fullmatch(r"[A-Za-z.]{2,6}", part) and item.unit is None:
                    item.unit = part
        if len(numbers) >= 3:
            item.quantity, item.unit_price, item.taxable_value = numbers[0], numbers[1], numbers[2]
        elif len(numbers) == 2:
            item.quantity, item.taxable_value = numbers[0], numbers[1]
        elif len(numbers) == 1:
            item.taxable_value = numbers[0]
        return item

    def _guess_supplier_name(self, lines: list[TextLine]) -> str | None:
        for line in lines[:5]:
            text = line.text.strip()
            if not text or re.search(r"(tax\s*invoice|invoice|gstin|original|duplicate|:)", text, re.I):
                continue
            if re.search(r"[A-Za-z]{3,}", text):
                return re.split(r"\s{2,}", text)[0]
        return None

    def _finalise_place_of_supply(self, inv: InvoiceData, result: ParseResult) -> None:
        if inv.place_of_supply:
            inv.place_of_supply_code = self.state_codes.code_for_name(inv.place_of_supply)

"""Presence and basic sanity checks for invoice fields (cf. CGST Rule 46)."""

from __future__ import annotations

import re
from datetime import date

from app.models.invoice import ExtractionResult
from app.models.validation import CheckCategory as Cat
from app.models.validation import CheckStatus as St
from app.models.validation import Severity
from app.validators.base import RuleOutput, make_check

_FIELD_LABELS = {
    "supplier_name": "Supplier name",
    "supplier_gstin": "Supplier GSTIN",
    "recipient_name": "Recipient name",
    "recipient_gstin": "Recipient GSTIN",
    "invoice_number": "Invoice number",
    "invoice_date": "Invoice date",
    "place_of_supply": "Place of supply",
    "line_items": "Line items",
    "taxable_value": "Taxable value",
    "cgst": "CGST",
    "sgst": "SGST",
    "igst": "IGST",
    "total_invoice_value": "Total invoice value",
}

# Missing any of these makes the document unusable as a tax invoice.
MANDATORY = ("supplier_name", "supplier_gstin", "invoice_number", "invoice_date", "line_items",
             "taxable_value", "total_invoice_value")

_INVOICE_NO_RE = re.compile(r"^[A-Za-z0-9/\-]{1,16}$")


def label(field: str) -> str:
    return _FIELD_LABELS.get(field, field)


def check_fields(extraction: ExtractionResult, today: date | None = None) -> RuleOutput:
    out = RuleOutput()
    inv = extraction.invoice
    today = today or date.today()

    for field in MANDATORY:
        value = getattr(inv, field)
        present = bool(value) if field == "line_items" else value is not None
        if field == "invoice_date" and value is None and inv.invoice_date_raw:
            out.add(make_check(
                "fields.invoice_date", Cat.FIELDS, "Invoice date", St.MANUAL_REVIEW,
                f"An invoice date was found ('{inv.invoice_date_raw}') but could not be read reliably.",
                field=field, actual=inv.invoice_date_raw,
                explanation="The date text is ambiguous or in an unrecognised format.",
                suggestion="Confirm the invoice date manually (DD-MM-YYYY).",
            ))
            continue
        if present:
            out.add(make_check(f"fields.{field}", Cat.FIELDS, label(field), St.PASS,
                               f"{label(field)} is present.", field=field))
        else:
            out.add(make_check(
                f"fields.{field}", Cat.FIELDS, label(field), St.MISSING,
                f"{label(field)} was not found on the invoice.", field=field,
                explanation=f"A GST tax invoice must show the {label(field).lower()}; "
                            "without it the invoice cannot be fully validated.",
                suggestion=f"Check whether the {label(field).lower()} is printed on the invoice. "
                           "If it is, enter it manually; if not, ask the supplier for a corrected invoice.",
            ))

    if not inv.recipient_name:
        out.add(make_check(
            "fields.recipient_name", Cat.FIELDS, "Recipient name", St.MISSING,
            "Recipient name was not found.", severity=Severity.WARNING, field="recipient_name",
            explanation="Rule 46 requires the recipient's name and address for registered recipients.",
            suggestion="Confirm the buyer's name is printed on the invoice.",
        ))
    else:
        out.add(make_check("fields.recipient_name", Cat.FIELDS, "Recipient name", St.PASS,
                           "Recipient name is present.", field="recipient_name"))

    if not inv.recipient_gstin:
        out.add(make_check(
            "fields.recipient_gstin", Cat.FIELDS, "Recipient GSTIN", St.MANUAL_REVIEW,
            "Recipient GSTIN was not found.", field="recipient_gstin",
            explanation="Recipient GSTIN is mandatory when the buyer is GST-registered (B2B). "
                        "It is not required for sales to unregistered buyers (B2C).",
            suggestion="If you are a registered business and want to claim input tax credit, "
                       "ask the supplier to add your GSTIN.",
        ))

    if inv.invoice_number and not _INVOICE_NO_RE.match(inv.invoice_number):
        out.add(make_check(
            "fields.invoice_number.format", Cat.FIELDS, "Invoice number format", St.WARNING,
            f"Invoice number '{inv.invoice_number}' is longer than 16 characters or contains "
            "characters other than letters, digits, '-' and '/'.",
            field="invoice_number", actual=inv.invoice_number,
            explanation="Rule 46 requires a consecutive serial number of up to 16 characters "
                        "using letters, digits, hyphen or slash.",
            suggestion="Verify the invoice number was read correctly.",
        ))

    if inv.invoice_date and inv.invoice_date > today:
        out.add(make_check(
            "fields.invoice_date.future", Cat.FIELDS, "Invoice date not in future", St.FAIL,
            f"Invoice date {inv.invoice_date.isoformat()} is in the future.",
            field="invoice_date", actual=inv.invoice_date.isoformat(),
            explanation="A tax invoice cannot be dated after the day it is checked.",
            suggestion="Check the date for a typing or OCR error and request a corrected invoice if needed.",
        ))

    for field in extraction.uncertain_fields:
        out.add(make_check(
            f"fields.{field}.uncertain", Cat.FIELDS, f"{label(field)} extraction confidence",
            St.MANUAL_REVIEW, f"{label(field)} was extracted with low confidence.", field=field,
            explanation="The value was read by OCR or suggested by the AI assistant and should be "
                        "compared with the original document.",
            suggestion=f"Compare the extracted {label(field).lower()} with the invoice image.",
        ))
    return out

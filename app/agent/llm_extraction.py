"""Optional LLM help for fields the rule-based parser missed.

Every value the LLM proposes must appear verbatim in the document text
(ignoring spaces, commas and case). Ungrounded values are discarded, so the LLM
cannot introduce information that is not on the invoice. Accepted values are
marked as uncertain and therefore sent to manual review.
"""

from __future__ import annotations

import logging
import re

from app.agent.llm_client import LLMClient
from app.agent.prompts import EXTRACT_SCHEMA, EXTRACT_SYSTEM, EXTRACTION_FIELDS
from app.core.exceptions import LLMError
from app.models.invoice import InvoiceData
from app.services.invoice_parser import parse_date
from app.utils.money import parse_amount

logger = logging.getLogger(__name__)

_AMOUNT_FIELDS = {"taxable_value", "cgst", "sgst", "igst", "total_invoice_value"}
MAX_TEXT_CHARS = 30_000


def _norm(value: str) -> str:
    return re.sub(r"[\s,₹]", "", value).lower()


def is_grounded(value: str, document_text: str) -> bool:
    needle = _norm(value)
    return bool(needle) and needle in _norm(document_text)


def fill_missing_fields(invoice: InvoiceData, document_text: str, llm: LLMClient) -> tuple[list[str], list[str]]:
    """Fill None fields in place. Returns (filled_fields, rejected_fields)."""
    missing = [f for f in EXTRACTION_FIELDS if getattr(invoice, f) is None]
    if not missing or not document_text.strip():
        return [], []
    try:
        data = llm.complete_json(
            EXTRACT_SYSTEM,
            f"<document_text>\n{document_text[:MAX_TEXT_CHARS]}\n</document_text>",
            EXTRACT_SCHEMA,
        )
    except LLMError as exc:
        logger.info("LLM-assisted extraction skipped: %s", exc)
        return [], []

    filled, rejected = [], []
    for field in missing:
        raw = data.get(field)
        if not isinstance(raw, str) or not raw.strip():
            continue
        raw = raw.strip()
        if not is_grounded(raw, document_text):
            rejected.append(field)
            continue
        if field in _AMOUNT_FIELDS:
            value = parse_amount(raw)
            if value is None:
                rejected.append(field)
                continue
            setattr(invoice, field, value)
        elif field == "invoice_date":
            invoice.invoice_date_raw = raw
            invoice.invoice_date = parse_date(raw)
        elif field.endswith("gstin"):
            setattr(invoice, field, re.sub(r"\s", "", raw).upper())
        else:
            setattr(invoice, field, raw)
        filled.append(field)
    return filled, rejected

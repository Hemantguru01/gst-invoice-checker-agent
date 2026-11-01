# Design notes

## Principles

1. **Deterministic rules decide; the LLM only explains.** Every `CheckStatus` and the overall status come from `app/validators`. The agent lets the LLM rewrite only the `explanation` and `suggestion` text, and only for check IDs that already exist.
2. **Never fabricate.** The parser leaves unknown fields as `None`. LLM-proposed extraction values are accepted only when they appear verbatim in the document text (`app/agent/llm_extraction.py::is_grounded`), and they are then marked uncertain.
3. **Uncertainty is a first-class result.** `MANUAL_REVIEW` is used when the tool cannot verify something: low-confidence reference rates, invoices dated before the reference data's effective date, an unknown supply type, and uncertain OCR fields.
4. **Reference data is versioned and labelled.** Rates live in JSON with a version, effective date, source and legal-status note, never in code.

## LLM guardrails (`app/agent/invoice_agent.py`)

- Only the findings are sent to the LLM, not the raw document.
- Responses must match a JSON schema (structured output).
- Items with unknown `check_id`s are ignored.
- `guard_text` rejects any explanation that:
  - contains a number not present in that finding's own text (so the LLM cannot invent rates or amounts), or
  - claims the invoice is "compliant", "approved for filing" or "ready to file".
- Rejected text is replaced by the rule-based explanation, and the count of rejections is reported in `llm_note`.
- Any `LLMError` (authentication, rate limit, network, refusal, truncation, invalid JSON) falls back to the rule-based explanations.

## Extraction pipeline

```
bytes → file_service.validate_upload (magic bytes, size, decode)
      → document_reader.read_document
            PDF: per page, PyMuPDF words → lines (column gaps become double spaces)
                 if the page has < 40 chars of text → render at 200 dpi → OCR
            image: OCR (upscaled when small)
      → invoice_parser.InvoiceParser (labels, sections, item table)
      → [optional] llm_extraction.fill_missing_fields (grounded)
      → ExtractionResult (missing_fields, uncertain_fields, field_sources)
```

## Supply type

`supplier GSTIN state code == place-of-supply state code` → intra-state (CGST + SGST); otherwise inter-state (IGST). If either side is unknown, the tool infers the supply type from the taxes charged, reports it as `MANUAL_REVIEW`, and records the inference under assumptions.

## Tax amount and invoice total checks

All tax and total checks use one set of recomputed figures (`ExpectedAmounts` in `app/validators/calculation.py`), so the expected values and differences in a report always come from the same source.

- Taxable base = Σ line taxable values as printed (less an invoice-level discount, if any). Quantity × price errors are reported by their own check and are not corrected here.
- Line rate = the rate printed on the line. If none is printed, the reference rate is used only for a **high-confidence** entry whose effective date covers the invoice date, and this is recorded as an assumption. The rate is **never derived from the tax charged**: comparing the tax with a rate worked back from that same tax would always pass. Without a usable rate, the tax check goes to manual review.
- Expected CGST = expected SGST = round(Σ taxable × rate ÷ 2), and expected total GST = CGST + SGST. For inter-state supplies, IGST = round(Σ taxable × rate).
- Two invoice-total checks, each labelled with its formula:
  - `calc.invoice_total` (*vs printed components*): printed taxable value + printed CGST/SGST/IGST + round-off.
  - `calc.invoice_total_expected` (*vs expected GST*): taxable base + expected GST + round-off.

  Example (the audited sample): taxable ₹1,07,000; expected CGST and SGST ₹6,780 each; printed SGST ₹6,700. The printed components add up to ₹1,20,480 and the expected total is ₹1,20,560, so both checks fail against the printed total of ₹1,25,000, each with its own difference.

Differences up to `ROUNDING_TOLERANCE` (₹1 by default) pass and are labelled as rounding. Special supplies (exports, SEZ, LUT) skip the domestic tax checks and go to manual review.

## Invoice numbers

`app/utils/invoice_number.py` keeps three forms apart:

- **raw**: exactly as printed; stored for audit and never changed.
- **display**: whitespace tidied around `/` and `-`.
- **key**: upper case, spaces removed, dash variants turned into `-`, and `/` and `-` kept.

The key is used for the Rule 46 format check and for duplicate detection: same supplier GSTIN, same key, and the same financial year (or an unknown one). A match is a WARNING; records are never merged. OCR word-space restoration skips tokens containing digits or separators, and the parser reads the invoice number from the unmodified OCR text.

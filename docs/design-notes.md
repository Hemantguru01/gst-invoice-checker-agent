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

## Tax amount check

Expected tax = Σ (line taxable value × line rate). The rate is the one printed on the line; for single-line invoices it is derived from the tax amounts, and otherwise the reference rate is used and recorded as an assumption. For intra-state supplies, CGST and SGST are each compared with half of the expected tax. Differences up to `ROUNDING_TOLERANCE` (₹1 by default) pass and are labelled as rounding.

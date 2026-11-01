"""Invoice checker agent.

Workflow:
  1. receive extracted invoice data
  2. run the deterministic rule engine
  3. collect errors, warnings and manual-review items
  4. ask the LLM (if configured) to explain findings in plain language
  5. attach suggested corrections
  6. assemble the structured validation report

The rule engine is the single source of truth for every status. The LLM may only
rewrite explanation and suggestion text, and its output passes guardrails that
reject new numbers, rates, or compliance claims. Without an LLM, or if any LLM
call fails, rule-based explanations are used.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timezone

from app.agent.llm_client import LLMClient
from app.agent.prompts import EXPLAIN_SCHEMA, EXPLAIN_SYSTEM
from app.core.exceptions import LLMError
from app.models.invoice import ExtractionResult
from app.models.validation import (
    AgentStep,
    CheckStatus,
    OverallStatus,
    ValidationCheck,
    ValidationReport,
)
from app.services.reference_data import HSNReference
from app.services.storage import DuplicateCandidate
from app.validators.engine import RuleEngine
from app.validators.gstin_rules import REGISTRATION_NOT_VERIFIED

logger = logging.getLogger(__name__)

_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_FORBIDDEN_CLAIMS = re.compile(
    r"\b(is|are|fully|completely)\s+(gst[\s-]?)?compliant\b|approved\s+for\s+filing|ready\s+(for|to)\s+fil",
    re.I,
)

# Reminders that apply to every invoice because the tool can never verify them automatically.
STANDING_MANUAL_ITEMS = [
    "Check whether e-invoicing (IRN/QR code) applies to the supplier based on their aggregate turnover.",
    "Check whether reverse charge applies to this supply.",
    "Confirm each GST rate against the current CBIC rate notification; the bundled reference dataset "
    "is a sample and is not legally authoritative.",
]


def gstin_registration_status(extraction: ExtractionResult) -> dict[str, str]:
    """Registration status per GSTIN. Never 'active': no authorised live source is integrated."""
    inv = extraction.invoice
    return {party: f"{gstin}: {REGISTRATION_NOT_VERIFIED}"
            for party, gstin in (("supplier", inv.supplier_gstin), ("recipient", inv.recipient_gstin)) if gstin}


def standing_reminders(extraction: ExtractionResult) -> list[str]:
    inv = extraction.invoice
    items = [f"{party.capitalize()} GSTIN {gstin}: registration and active status NOT verified (no authorised "
             "live source). Confirm it is active, not cancelled or suspended, with 'Search Taxpayer' on the "
             "GST portal."
             for party, gstin in (("supplier", inv.supplier_gstin), ("recipient", inv.recipient_gstin)) if gstin]
    if not items:
        items.append("Confirm the supplier's GSTIN and its registration status on the GST portal.")
    items.extend(STANDING_MANUAL_ITEMS)
    if extraction.ocr_used:
        items.append(f"Values were read by OCR ({extraction.method_label}). Compare GSTINs, dates and "
                     "amounts with the original document before relying on them.")
    return items


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".").removesuffix(".00") for n in _NUMBER_RE.findall(text)}


def guard_text(candidate: str, allowed_context: str) -> bool:
    """True if the LLM text introduces no new numbers and makes no compliance claims."""
    if not candidate.strip() or _FORBIDDEN_CLAIMS.search(candidate):
        return False
    allowed = _numbers(allowed_context)
    for number in _numbers(candidate):
        if number not in allowed and not (number.isdigit() and int(number) <= 3):
            return False
    return True


class InvoiceCheckerAgent:
    def __init__(self, engine: RuleEngine, hsn_reference: HSNReference, llm: LLMClient | None = None) -> None:
        self.engine = engine
        self.hsn_reference = hsn_reference
        self.llm = llm

    def run(self, invoice_id: str, extraction: ExtractionResult, *, original_filename: str | None = None,
            use_llm: bool = True, today: date | None = None,
            duplicates: list[DuplicateCandidate] | None = None) -> ValidationReport:
        trace: list[AgentStep] = []
        inv = extraction.invoice
        present = sum(1 for f in ("supplier_gstin", "invoice_number", "invoice_date", "total_invoice_value")
                      if getattr(inv, f) is not None)
        trace.append(AgentStep(step="1. Receive invoice data",
                               detail=f"Extraction status '{extraction.status.value}' via {extraction.method}; "
                                      f"{len(inv.line_items)} line item(s); {len(extraction.missing_fields)} "
                                      f"field(s) missing; {present}/4 key fields present."))

        result = self.engine.run(extraction, today=today, duplicates=duplicates)
        checks = result.checks
        trace.append(AgentStep(step="2. Run deterministic rules",
                               detail=f"{len(checks)} checks executed (GSTIN, HSN/SAC, rates, calculations, fields)."))

        issues = [c for c in checks if c.status is not CheckStatus.PASS]
        summary = result.summary
        assert summary is not None
        findings = [f"{c.name}: {c.message}" for c in checks if c.status is CheckStatus.MANUAL_REVIEW]
        reminders = standing_reminders(extraction)
        summary.standing_reminders = len(reminders)
        summary.manual_verification_total = len(findings) + len(reminders)
        trace.append(AgentStep(step="3. Collect findings",
                               detail=f"{summary.errors} error(s), {summary.warnings} warning(s), "
                                      f"{summary.manual_review} automated manual-review finding(s), "
                                      f"{summary.standing_reminders} standing verification reminder(s), "
                                      f"{summary.passed} passed."))

        overall_explanation = self._overall_explanation(result.overall_status, summary.errors,
                                                         summary.warnings, summary.manual_review,
                                                         summary.standing_reminders)
        llm_used, llm_note = False, None
        if self.llm is None:
            mode = "Rule-based fallback: no Claude API key configured"
        elif not use_llm:
            mode = "Rule-based: Claude explanations turned off for this request"
        else:
            mode = "Rule-based: no issues needed explaining"
        if not issues:
            trace.append(AgentStep(step="4. Explain findings", detail="No issues to explain."))
        elif self.llm is None or not use_llm:
            llm_note = ("LLM not configured (no ANTHROPIC_API_KEY); explanations come from the rule engine."
                        if self.llm is None else "LLM disabled for this request; rule-based explanations used.")
            trace.append(AgentStep(step="4. Explain findings", detail=llm_note))
        else:
            try:
                accepted, rejected, ai_summary = self._llm_explain(issues, result.overall_status)
                llm_used = accepted > 0 or ai_summary is not None
                if ai_summary:
                    overall_explanation = f"{overall_explanation} {ai_summary}"
                llm_note = (f"LLM explanations accepted for {accepted} finding(s); {rejected} rejected "
                            "by guardrails and replaced with rule-based text.")
                mode = (f"Claude ({getattr(self.llm, 'model', 'LLM')}): plain-language explanations only; "
                        "statuses from the rule engine" if llm_used
                        else "Rule-based fallback: all Claude explanations were rejected by guardrails")
            except LLMError as exc:
                llm_note = f"LLM unavailable ({exc}); rule-based explanations used."
                mode = "Rule-based fallback: Claude call failed"
                logger.info("LLM fallback: %s", exc)
            trace.append(AgentStep(step="4. Explain findings", detail=llm_note or ""))

        for check in issues:
            check.explanation = check.explanation or check.message
            check.suggestion = check.suggestion or "Review this item against the original invoice."
        trace.append(AgentStep(step="5. Suggest corrections",
                               detail=f"Suggestions attached to {len(issues)} finding(s)."))

        report = ValidationReport(
            invoice_id=invoice_id,
            original_filename=original_filename,
            generated_at=datetime.now(timezone.utc),
            overall_status=result.overall_status,
            summary=summary,
            overall_explanation=overall_explanation,
            checks=checks,
            manual_verification=findings + reminders,
            manual_review_findings=findings,
            standing_reminders=reminders,
            gstin_registration=gstin_registration_status(extraction),
            assumptions=result.assumptions,
            extraction=extraction,
            reference_data_version=self.hsn_reference.version,
            llm_used=llm_used,
            llm_model=getattr(self.llm, "model", None) if llm_used else None,
            llm_note=llm_note,
            explanation_mode=mode,
            agent_trace=trace,
        )
        report.agent_trace.append(AgentStep(step="6. Build report",
                                            detail=f"Overall status: {report.overall_status.value}."))
        return report

    @staticmethod
    def _overall_explanation(status: OverallStatus, errors: int, warnings: int, manual: int,
                             standing: int) -> str:
        if status is OverallStatus.VALID:
            return (f"All automated checks passed. This does not certify GST compliance: {standing} standing "
                    "verification reminders still need a person, including GSTIN registration status.")
        if status is OverallStatus.INVALID:
            return (f"The invoice has {errors} error(s) that should be corrected before it is relied on "
                    f"for input tax credit, plus {warnings} warning(s). {manual} automated finding(s) need "
                    f"manual review, and {standing} standing verification reminders apply.")
        return (f"No definite errors were found, but {warnings} warning(s) and {manual} automated finding(s) "
                f"need manual review, in addition to {standing} standing verification reminders.")

    def _llm_explain(self, issues: list[ValidationCheck], status: OverallStatus) -> tuple[int, int, str | None]:
        assert self.llm is not None
        findings = [
            {
                "check_id": c.check_id, "name": c.name, "status": c.status.value,
                "severity": c.severity.value, "finding": c.message, "expected": c.expected,
                "actual": c.actual, "rule_explanation": c.explanation, "rule_suggestion": c.suggestion,
            }
            for c in issues
        ]
        user = (f"Overall status decided by the rule engine: {status.value}.\n"
                f"Findings (JSON):\n{json.dumps(findings, ensure_ascii=False, indent=1)}")
        data = self.llm.complete_json(EXPLAIN_SYSTEM, user, EXPLAIN_SCHEMA)

        by_id = {c.check_id: c for c in issues}
        accepted = rejected = 0
        for item in data.get("items", []):
            if not isinstance(item, dict):
                continue
            check = by_id.get(str(item.get("check_id")))
            if check is None:
                continue  # unknown IDs are ignored: the LLM cannot add findings
            context = " ".join(filter(None, [check.message, check.expected, check.actual,
                                             check.explanation, check.suggestion]))
            explanation = str(item.get("explanation", ""))
            suggestion = str(item.get("suggestion", ""))
            if guard_text(explanation, context) and guard_text(suggestion, context):
                check.explanation, check.suggestion = explanation, suggestion
                check.explanation_source = "llm"
                accepted += 1
            else:
                rejected += 1

        all_context = " ".join(f"{c.message} {c.expected or ''} {c.actual or ''}" for c in issues)
        all_context += " " + " ".join(str(n) for n in range(len(issues) + 10))  # counts of findings/reminders
        summary = str(data.get("summary", "")).strip()
        ai_summary = summary if summary and guard_text(summary, all_context) else None
        return accepted, rejected, ai_summary

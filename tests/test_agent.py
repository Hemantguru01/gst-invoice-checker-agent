from decimal import Decimal

from app.agent.invoice_agent import InvoiceCheckerAgent, guard_text
from app.agent.llm_client import build_llm_client
from app.core.config import Settings
from app.core.exceptions import LLMError
from app.models.validation import CheckStatus, OverallStatus
from tests.conftest import TODAY, as_extraction, intra_state_invoice

D = Decimal


class FailingLLM:
    model = "fake-down"

    def complete_json(self, system, user, schema):
        raise LLMError("Could not reach the LLM API.")


class ScriptedLLM:
    model = "fake-llm"

    def __init__(self, builder):
        self.builder = builder
        self.calls = 0

    def complete_json(self, system, user, schema):
        self.calls += 1
        return self.builder(user)


def _bad_invoice():
    return intra_state_invoice(total_invoice_value=D("180000"))


def _agent(engine, hsn_ref, llm=None):
    return InvoiceCheckerAgent(engine, hsn_ref, llm)


def test_no_llm_configured_uses_rule_based_fallback(engine, hsn_ref):
    report = _agent(engine, hsn_ref).run("id-1", as_extraction(_bad_invoice()), today=TODAY)
    assert report.overall_status is OverallStatus.INVALID
    assert report.llm_used is False
    assert "not configured" in report.llm_note
    failed = [c for c in report.checks if c.status is CheckStatus.FAIL]
    assert failed and all(c.explanation and c.suggestion for c in failed)
    assert all(c.explanation_source == "rules" for c in report.checks)


def test_settings_without_key_build_no_client():
    assert build_llm_client(Settings(_env_file=None, anthropic_api_key=None)) is None
    assert build_llm_client(Settings(_env_file=None, anthropic_api_key="x", llm_provider="none")) is None


def test_llm_failure_falls_back(engine, hsn_ref):
    report = _agent(engine, hsn_ref, FailingLLM()).run("id-2", as_extraction(_bad_invoice()), today=TODAY)
    assert report.overall_status is OverallStatus.INVALID
    assert report.llm_used is False
    assert "LLM unavailable" in report.llm_note
    assert any(c.status is CheckStatus.FAIL and c.explanation for c in report.checks)


def test_llm_cannot_override_status_or_add_findings(engine, hsn_ref):
    def builder(_user):
        return {
            "summary": "Everything looks fine.",
            "items": [
                {"check_id": "calc.invoice_total", "explanation": "The total is a little off.",
                 "suggestion": "Ask the supplier to fix the total."},
                {"check_id": "invented.check", "explanation": "New problem", "suggestion": "x"},
            ],
        }

    report = _agent(engine, hsn_ref, ScriptedLLM(builder)).run("id-3", as_extraction(_bad_invoice()), today=TODAY)
    total = next(c for c in report.checks if c.check_id == "calc.invoice_total")
    assert total.status is CheckStatus.FAIL  # unchanged
    assert total.explanation_source == "llm"
    assert report.overall_status is OverallStatus.INVALID
    assert not any(c.check_id == "invented.check" for c in report.checks)


def test_llm_text_with_invented_rate_or_compliance_claim_is_rejected(engine, hsn_ref):
    def builder(_user):
        return {
            "summary": "This invoice is fully compliant and approved for filing.",
            "items": [{"check_id": "calc.invoice_total",
                       "explanation": "Laptops are taxed at 28% so the total should be 192000.",
                       "suggestion": "Charge 28%."}],
        }

    report = _agent(engine, hsn_ref, ScriptedLLM(builder)).run("id-4", as_extraction(_bad_invoice()), today=TODAY)
    total = next(c for c in report.checks if c.check_id == "calc.invoice_total")
    assert total.explanation_source == "rules"
    assert "28%" not in (total.explanation or "")
    assert "compliant" not in report.overall_explanation
    assert "1 rejected" in report.llm_note


def test_llm_not_called_for_clean_invoice(engine, hsn_ref):
    llm = ScriptedLLM(lambda _u: {"summary": "", "items": []})
    report = _agent(engine, hsn_ref, llm).run("id-5", as_extraction(intra_state_invoice()), today=TODAY)
    assert report.overall_status is OverallStatus.VALID
    assert llm.calls == 0


def test_use_llm_false_skips_llm(engine, hsn_ref):
    llm = ScriptedLLM(lambda _u: {"summary": "", "items": []})
    report = _agent(engine, hsn_ref, llm).run("id-6", as_extraction(_bad_invoice()), use_llm=False, today=TODAY)
    assert llm.calls == 0
    assert "disabled" in report.llm_note


def test_report_structure(engine, hsn_ref):
    report = _agent(engine, hsn_ref).run("id-7", as_extraction(intra_state_invoice(recipient_gstin=None)), today=TODAY)
    assert report.overall_status is OverallStatus.NEEDS_REVIEW
    assert [s.step[:2] for s in report.agent_trace] == ["1.", "2.", "3.", "4.", "5.", "6."]
    assert any("GST portal" in m for m in report.manual_verification)
    assert "not a certification" in report.disclaimer
    assert report.summary.total_checks == len(report.checks)


def test_guard_text():
    ctx = "Total should be ₹1,77,000.00 but the invoice shows ₹1,80,000.00"
    assert guard_text("The total should be 177000 rupees.", ctx)
    assert not guard_text("Use 12% GST instead.", ctx)
    assert not guard_text("This invoice is GST compliant.", ctx)
    assert not guard_text("", ctx)

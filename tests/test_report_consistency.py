"""JSON, Markdown and HTML reports must agree; the explanation source must always be visible."""

import html

import pytest

from app.agent.invoice_agent import InvoiceCheckerAgent
from app.core.exceptions import LLMError
from app.models.validation import CheckStatus
from app.services.extraction_service import ExtractionService
from app.services.report_service import to_html, to_markdown
from app.utils.sample_invoice import export_invoice, invalid_invoice, render_pdf, valid_intra_state
from tests.conftest import TODAY


class OneLineLLM:
    model = "fake-claude"

    def complete_json(self, system, user, schema):
        return {"items": [], "summary": "The invoice has problems that need correcting."}


class DownLLM:
    model = "fake-claude"

    def complete_json(self, system, user, schema):
        raise LLMError("network down")


def _report(engine, hsn_ref, factory, llm=None):
    extraction = ExtractionService(None, None).extract(render_pdf(factory()), "pdf")
    return InvoiceCheckerAgent(engine, hsn_ref, llm).run("00000000-0000-4000-8000-000000000001", extraction,
                                                         original_filename="sample.pdf", today=TODAY)


@pytest.mark.parametrize("factory", [valid_intra_state, invalid_invoice, export_invoice])
def test_all_formats_agree(engine, hsn_ref, factory):
    report = _report(engine, hsn_ref, factory)
    md, page = to_markdown(report), html.unescape(to_html(report))
    s = report.summary

    for text in (md, page):
        assert report.overall_status.value in text
        assert report.extraction.method_label in text
        assert report.explanation_mode in text
        for check in report.checks:  # every status and finding, including totals and expected values
            assert check.message in text, check.check_id
            if check.expected:
                assert check.expected in text
        for item in report.manual_review_findings + report.standing_reminders:
            assert item in text
        assert f"Automated manual-review findings ({s.manual_review})" in text
        assert f"Standing verification reminders ({s.standing_reminders})" in text

    cells = [s.total_checks, s.passed, s.errors, s.warnings, s.manual_review, s.standing_reminders,
             s.manual_verification_total]
    assert "| " + " | ".join(str(v) for v in cells) + " |" in md
    assert "".join(f"<td>{v}</td>" for v in cells) in page

    # Summary counts match the check list the dashboard renders from the same JSON.
    assert s.total_checks == len(report.checks)
    assert s.passed == sum(c.status is CheckStatus.PASS for c in report.checks)
    assert s.manual_review == sum(c.status is CheckStatus.MANUAL_REVIEW for c in report.checks)
    assert s.manual_verification_total == len(report.manual_verification)


def test_json_round_trip_keeps_computed_fields(engine, hsn_ref):
    report = _report(engine, hsn_ref, valid_intra_state)
    data = report.model_dump(mode="json")
    assert data["extraction"]["method_label"] == report.extraction.method_label
    assert data["extraction"]["ocr_used"] is False
    assert type(report).model_validate(data).summary == report.summary  # stored reports load back


def test_rule_based_fallback_is_named(engine, hsn_ref):
    report = _report(engine, hsn_ref, invalid_invoice)
    assert report.llm_used is False
    assert report.explanation_mode == "Rule-based fallback: no Claude API key configured"
    assert "no ANTHROPIC_API_KEY" in report.llm_note


def test_claude_mode_is_named_when_used(engine, hsn_ref):
    report = _report(engine, hsn_ref, invalid_invoice, OneLineLLM())
    assert report.llm_used is True and report.llm_model == "fake-claude"
    assert report.explanation_mode.startswith("Claude (fake-claude)")
    assert "statuses from the rule engine" in report.explanation_mode


def test_failed_claude_call_is_named_as_fallback(engine, hsn_ref):
    report = _report(engine, hsn_ref, invalid_invoice, DownLLM())
    assert report.llm_used is False
    assert report.explanation_mode == "Rule-based fallback: Claude call failed"

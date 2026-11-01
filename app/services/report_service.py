"""Renders validation reports as JSON, Markdown or standalone HTML."""

from __future__ import annotations

import html
from pathlib import Path

from app.models.validation import CheckStatus, ValidationReport
from app.utils.money import fmt_inr

STATUS_ICON = {
    CheckStatus.PASS: "✅",
    CheckStatus.FAIL: "❌",
    CheckStatus.MISSING: "⚠️",
    CheckStatus.WARNING: "⚠️",
    CheckStatus.MANUAL_REVIEW: "🔍",
}

_INVOICE_ROWS = (
    ("Supplier", "supplier_name"), ("Supplier GSTIN", "supplier_gstin"),
    ("Recipient", "recipient_name"), ("Recipient GSTIN", "recipient_gstin"),
    ("Invoice number", "invoice_number"), ("Invoice date", "invoice_date"),
    ("Place of supply", "place_of_supply"),
)
_AMOUNT_ROWS = (("Taxable value", "taxable_value"), ("CGST", "cgst"), ("SGST", "sgst"),
                ("IGST", "igst"), ("Round-off", "round_off"), ("Total invoice value", "total_invoice_value"))


def _md_cell(value: object) -> str:
    return str(value if value not in (None, "") else "—").replace("|", "\\|").replace("\n", " ")


def to_markdown(report: ValidationReport) -> str:
    inv = report.extraction.invoice
    s = report.summary
    out = [
        "# GST Invoice Validation Report", "",
        f"**Overall status:** {report.overall_status.value}  ",
        f"**File:** {report.original_filename or '—'}  ",
        f"**Invoice ID:** {report.invoice_id}  ",
        f"**Generated:** {report.generated_at:%Y-%m-%d %H:%M UTC}  ",
        f"**Reference data:** {report.reference_data_version}  ",
        f"**Explanations:** {'LLM-assisted (' + (report.llm_model or '') + ')' if report.llm_used else 'rule-based'}",
        "", report.overall_explanation, "",
        "| Checks | Passed | Errors | Warnings | Manual review |", "|---|---|---|---|---|",
        f"| {s.total_checks} | {s.passed} | {s.errors} | {s.warnings} | {s.manual_review} |", "",
        "## Extracted invoice details", "", "| Field | Value |", "|---|---|",
    ]
    out += [f"| {lbl} | {_md_cell(getattr(inv, f))} |" for lbl, f in _INVOICE_ROWS]
    out += [f"| {lbl} | {fmt_inr(getattr(inv, f)) if getattr(inv, f) is not None else '—'} |" for lbl, f in _AMOUNT_ROWS]
    if inv.line_items:
        out += ["", "### Line items", "", "| # | Description | HSN/SAC | Qty | Unit price | Taxable value | Rate |",
                "|---|---|---|---|---|---|---|"]
        for i, li in enumerate(inv.line_items, 1):
            out.append(f"| {i} | {_md_cell(li.description)} | {_md_cell(li.hsn_sac)} | {_md_cell(li.quantity)} | "
                       f"{fmt_inr(li.unit_price)} | {fmt_inr(li.taxable_value)} | "
                       f"{_md_cell(f'{li.gst_rate}%' if li.gst_rate is not None else None)} |")
    if report.extraction.missing_fields:
        out += ["", f"**Missing fields:** {', '.join(report.extraction.missing_fields)}"]

    issues = [c for c in report.checks if c.status is not CheckStatus.PASS]
    out += ["", "## Issues", ""]
    if not issues:
        out.append("No issues found by the automated checks.")
    for c in issues:
        out += [f"### {STATUS_ICON[c.status]} {c.name} — {c.status.value}", "",
                f"- **Finding:** {c.message}"]
        if c.expected or c.actual:
            out.append(f"- **Expected:** {c.expected or '—'} · **Found:** {c.actual or '—'}")
        out += [f"- **Explanation:** {c.explanation or '—'}", f"- **Suggested correction:** {c.suggestion or '—'}", ""]

    out += ["## All checks", "", "| Status | Category | Check | Result |", "|---|---|---|---|"]
    out += [f"| {STATUS_ICON[c.status]} {c.status.value} | {c.category.value} | {_md_cell(c.name)} | {_md_cell(c.message)} |"
            for c in report.checks]
    out += ["", "## Manual verification required", ""] + [f"- {m}" for m in report.manual_verification]
    out += ["", "## Assumptions", ""] + [f"- {a}" for a in report.assumptions]
    out += ["", "## Disclaimer", "", report.disclaimer, ""]
    return "\n".join(out)


def to_html(report: ValidationReport) -> str:
    colors = {"VALID": "#15803d", "INVALID": "#b91c1c", "NEEDS REVIEW": "#b45309"}
    color = colors.get(report.overall_status.value, "#334155")
    rows = "".join(
        f"<tr><td>{STATUS_ICON[c.status]} {c.status.value}</td><td>{html.escape(c.category.value)}</td>"
        f"<td>{html.escape(c.name)}</td><td>{html.escape(c.message)}</td>"
        f"<td>{html.escape(c.explanation or '') if c.status is not CheckStatus.PASS else ''}</td>"
        f"<td>{html.escape(c.suggestion or '') if c.status is not CheckStatus.PASS else ''}</td></tr>"
        for c in report.checks
    )
    inv = report.extraction.invoice
    details = "".join(f"<tr><th>{lbl}</th><td>{html.escape(str(getattr(inv, f) or '—'))}</td></tr>"
                      for lbl, f in _INVOICE_ROWS)
    details += "".join(f"<tr><th>{lbl}</th><td>{fmt_inr(getattr(inv, f)) if getattr(inv, f) is not None else '—'}</td></tr>"
                       for lbl, f in _AMOUNT_ROWS)
    manual = "".join(f"<li>{html.escape(m)}</li>" for m in report.manual_verification)
    s = report.summary
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GST Validation Report</title>
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:24px;color:#0f172a;background:#fff;line-height:1.45}}
.badge{{display:inline-block;padding:4px 12px;border-radius:6px;color:#fff;background:{color};font-weight:600}}
table{{border-collapse:collapse;width:100%;margin:12px 0;font-size:14px}}
td,th{{border:1px solid #e2e8f0;padding:6px 8px;text-align:left;vertical-align:top}}
th{{background:#f8fafc}} .note{{color:#475569;font-size:13px}}
</style></head><body>
<h1>GST Invoice Validation Report</h1>
<p><span class="badge">{report.overall_status.value}</span></p>
<p>{html.escape(report.overall_explanation)}</p>
<p class="note">File: {html.escape(report.original_filename or '—')} · Generated {report.generated_at:%Y-%m-%d %H:%M UTC}
· Reference data {html.escape(report.reference_data_version)}</p>
<table><tr><th>Checks</th><th>Passed</th><th>Errors</th><th>Warnings</th><th>Manual review</th></tr>
<tr><td>{s.total_checks}</td><td>{s.passed}</td><td>{s.errors}</td><td>{s.warnings}</td><td>{s.manual_review}</td></tr></table>
<h2>Extracted details</h2><table>{details}</table>
<h2>Checks</h2><table><tr><th>Status</th><th>Category</th><th>Check</th><th>Finding</th><th>Explanation</th><th>Suggested correction</th></tr>{rows}</table>
<h2>Manual verification</h2><ul>{manual}</ul>
<p class="note">{html.escape(report.disclaimer)}</p>
</body></html>"""


def save_report_files(report: ValidationReport, reports_dir: Path) -> dict[str, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": reports_dir / f"{report.invoice_id}.json",
        "md": reports_dir / f"{report.invoice_id}.md",
    }
    paths["json"].write_text(report.model_dump_json(indent=2), encoding="utf-8")
    paths["md"].write_text(to_markdown(report), encoding="utf-8")
    return paths

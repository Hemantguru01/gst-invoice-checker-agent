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
    ("Invoice number", "invoice_number"), ("Invoice number (as printed)", "invoice_number_raw"),
    ("Invoice date", "invoice_date"),
    ("Place of supply", "place_of_supply"),
)
_AMOUNT_ROWS = (("Taxable value", "taxable_value"), ("CGST", "cgst"), ("SGST", "sgst"),
                ("IGST", "igst"), ("Round-off", "round_off"), ("Total invoice value", "total_invoice_value"))


def _md_cell(value: object) -> str:
    return str(value if value not in (None, "") else "—").replace("|", "\\|").replace("\n", " ")


def _meta_rows(report: ValidationReport) -> list[tuple[str, str]]:
    """Report metadata shared by every format, so they cannot drift apart."""
    return [
        ("File", report.original_filename or "—"),
        ("Invoice ID", report.invoice_id),
        ("Generated", f"{report.generated_at:%Y-%m-%d %H:%M UTC}"),
        ("Extraction method", report.extraction.method_label),
        ("Explanations", report.explanation_mode),
        ("Reference data", f"{report.reference_data_version} (sample dataset, not legally authoritative)"),
    ] + [(f"{party.capitalize()} GSTIN registration", status) for party, status in report.gstin_registration.items()]


def _summary_cells(report: ValidationReport) -> list[tuple[str, int]]:
    s = report.summary
    return [("Checks", s.total_checks), ("Passed", s.passed), ("Errors", s.errors), ("Warnings", s.warnings),
            ("Manual-review findings", s.manual_review), ("Standing reminders", s.standing_reminders),
            ("Human verification items", s.manual_verification_total)]


def _manual_sections(report: ValidationReport) -> list[tuple[str, list[str]]]:
    findings, reminders = report.manual_review_findings, report.standing_reminders
    if not findings and not reminders:  # reports saved before the split
        reminders = report.manual_verification
    return [(f"Automated manual-review findings ({len(findings)})",
             findings or ["None from the automated checks; the standing reminders below still apply."]),
            (f"Standing verification reminders ({len(reminders)})", reminders)]


def _amount(inv: object, attr: str) -> str:
    value = getattr(inv, attr)
    return fmt_inr(value) if value is not None else "—"


def _line_rows(report: ValidationReport) -> list[list[str]]:
    return [[str(i), li.description or "—", li.hsn_sac or "—",
             str(li.quantity) if li.quantity is not None else "—", fmt_inr(li.unit_price) if li.unit_price is not None else "—",
             fmt_inr(li.taxable_value) if li.taxable_value is not None else "—",
             f"{li.gst_rate}%" if li.gst_rate is not None else "—"]
            for i, li in enumerate(report.extraction.invoice.line_items, 1)]


_LINE_HEADERS = ["#", "Description", "HSN/SAC", "Qty", "Unit price", "Taxable value", "Rate"]


def to_markdown(report: ValidationReport) -> str:
    inv = report.extraction.invoice
    cells = _summary_cells(report)
    out = ["# GST Invoice Validation Report", "", f"**Overall status:** {report.overall_status.value}  "]
    out += [f"**{label}:** {_md_cell(value)}  " for label, value in _meta_rows(report)]
    out += ["", report.overall_explanation, "",
            "| " + " | ".join(lbl for lbl, _ in cells) + " |", "|" + "---|" * len(cells),
            "| " + " | ".join(str(v) for _, v in cells) + " |", "",
            "## Extracted invoice details", "", "| Field | Value |", "|---|---|"]
    out += [f"| {lbl} | {_md_cell(getattr(inv, f))} |" for lbl, f in _INVOICE_ROWS]
    out += [f"| {lbl} | {_amount(inv, f)} |" for lbl, f in _AMOUNT_ROWS]
    if inv.line_items:
        out += ["", "### Line items", "", "| " + " | ".join(_LINE_HEADERS) + " |", "|" + "---|" * len(_LINE_HEADERS)]
        out += ["| " + " | ".join(_md_cell(c) for c in row) + " |" for row in _line_rows(report)]
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

    out += ["## All checks", "", "| Status | Category | Check | Result | Expected | Found |", "|---|---|---|---|---|---|"]
    out += [f"| {STATUS_ICON[c.status]} {c.status.value} | {c.category.value} | {_md_cell(c.name)} | "
            f"{_md_cell(c.message)} | {_md_cell(c.expected)} | {_md_cell(c.actual)} |"
            for c in report.checks]
    out += ["", "## Manual verification required"]
    for title, items in _manual_sections(report):
        out += ["", f"### {title}", ""] + [f"- {m}" for m in items]
    out += ["", "## Assumptions", ""] + [f"- {a}" for a in report.assumptions]
    out += ["", "## Disclaimer", "", report.disclaimer, ""]
    return "\n".join(out)


def to_html(report: ValidationReport) -> str:
    esc = html.escape
    colors = {"VALID": "#15803d", "INVALID": "#b91c1c", "NEEDS REVIEW": "#b45309"}
    color = colors.get(report.overall_status.value, "#334155")
    inv = report.extraction.invoice
    meta = "".join(f"<tr><th>{esc(lbl)}</th><td>{esc(str(v))}</td></tr>" for lbl, v in _meta_rows(report))
    cells = _summary_cells(report)
    summary = ("<tr>" + "".join(f"<th>{esc(lbl)}</th>" for lbl, _ in cells) + "</tr><tr>"
               + "".join(f"<td>{v}</td>" for _, v in cells) + "</tr>")
    details = "".join(f"<tr><th>{lbl}</th><td>{esc(str(getattr(inv, f) or '—'))}</td></tr>"
                      for lbl, f in _INVOICE_ROWS)
    details += "".join(f"<tr><th>{lbl}</th><td>{_amount(inv, f)}</td></tr>" for lbl, f in _AMOUNT_ROWS)
    lines = ""
    if inv.line_items:
        lines = ("<h3>Line items</h3><table><tr>" + "".join(f"<th>{h}</th>" for h in _LINE_HEADERS) + "</tr>"
                 + "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in row) + "</tr>" for row in _line_rows(report))
                 + "</table>")
    missing = (f"<p><strong>Missing fields:</strong> {esc(', '.join(report.extraction.missing_fields))}</p>"
               if report.extraction.missing_fields else "")
    rows = "".join(
        f"<tr><td>{STATUS_ICON[c.status]} {c.status.value}</td><td>{esc(c.category.value)}</td>"
        f"<td>{esc(c.name)}</td><td>{esc(c.message)}</td><td>{esc(c.expected or '—')}</td><td>{esc(c.actual or '—')}</td>"
        f"<td>{esc(c.explanation or '') if c.status is not CheckStatus.PASS else ''}</td>"
        f"<td>{esc(c.suggestion or '') if c.status is not CheckStatus.PASS else ''}</td></tr>"
        for c in report.checks
    )
    manual = "".join(f"<h3>{esc(title)}</h3><ul>" + "".join(f"<li>{esc(m)}</li>" for m in items) + "</ul>"
                     for title, items in _manual_sections(report))
    assumptions = "".join(f"<li>{esc(a)}</li>" for a in report.assumptions)
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
<p>{esc(report.overall_explanation)}</p>
<table>{meta}</table>
<table>{summary}</table>
<h2>Extracted details</h2><table>{details}</table>{lines}{missing}
<h2>Checks</h2><table><tr><th>Status</th><th>Category</th><th>Check</th><th>Finding</th><th>Expected</th><th>Found</th><th>Explanation</th><th>Suggested correction</th></tr>{rows}</table>
<h2>Manual verification required</h2>{manual}
<h2>Assumptions</h2><ul>{assumptions}</ul>
<h2>Disclaimer</h2><p class="note">{esc(report.disclaimer)}</p>
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


def delete_report_files(invoice_id: str, reports_dir: Path) -> None:
    root = reports_dir.resolve()
    for ext in (".json", ".md"):
        path = (root / f"{invoice_id}{ext}").resolve()
        if path.parent == root and path.is_file():
            path.unlink()

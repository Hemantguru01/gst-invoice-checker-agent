"""Streamlit dashboard for the GST Invoice Checker Agent.

Run with:  streamlit run frontend/streamlit_app.py
(the FastAPI backend must be running; see README).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import pymupdf
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.utils.money import fmt_inr, parse_amount  # noqa: E402
from frontend.api_client import DEFAULT_BASE_URL, APIError, GSTApiClient  # noqa: E402

st.set_page_config(page_title="GST Invoice Checker", page_icon="🧾", layout="wide")

STATUS_STYLE = {
    "VALID": ("#15803d", "#dcfce7", "All automated checks passed"),
    "INVALID": ("#b91c1c", "#fee2e2", "Errors found"),
    "NEEDS REVIEW": ("#b45309", "#fef3c7", "Manual verification needed"),
}
CHECK_ICON = {"PASS": "✅", "FAIL": "❌", "MISSING": "⚠️", "WARNING": "⚠️", "MANUAL_REVIEW": "🔍"}
FIELD_LABELS = [
    ("supplier_name", "Supplier name"), ("supplier_gstin", "Supplier GSTIN"),
    ("recipient_name", "Recipient name"), ("recipient_gstin", "Recipient GSTIN"),
    ("invoice_number", "Invoice number"), ("invoice_date", "Invoice date"),
    ("place_of_supply", "Place of supply"),
]
AMOUNT_LABELS = [
    ("taxable_value", "Taxable value"), ("cgst", "CGST"), ("sgst", "SGST"), ("igst", "IGST"),
    ("round_off", "Round-off"), ("total_invoice_value", "Total invoice value"),
]

st.markdown(
    """<style>
    .status-card{padding:14px 18px;border-radius:8px;margin-bottom:8px;border:1px solid rgba(0,0,0,.08)}
    .status-card h3{margin:0;font-size:1.35rem}
    .status-card p{margin:4px 0 0 0}
    .disclaimer{font-size:.82rem;color:#64748b}
    </style>""",
    unsafe_allow_html=True,
)


@st.cache_resource
def get_client(base_url: str) -> GSTApiClient:
    return GSTApiClient(base_url)


def inr(value: Any) -> str:
    if value in (None, ""):
        return "—"
    amount = parse_amount(value)
    return fmt_inr(amount) if amount is not None else str(value)


def render_preview(content: bytes, name: str) -> None:
    if name.lower().endswith(".pdf"):
        try:
            with pymupdf.open(stream=content, filetype="pdf") as doc:
                for index, page in enumerate(doc):
                    if index >= 3:
                        st.caption(f"Showing first 3 of {doc.page_count} pages.")
                        break
                    st.image(page.get_pixmap(dpi=110).tobytes("png"), use_container_width=True)
        except Exception:  # noqa: BLE001
            st.warning("Preview unavailable for this PDF.")
    else:
        st.image(content, use_container_width=True)


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("🧾 GST Invoice Checker")
    base_url = st.text_input("Backend URL", value=DEFAULT_BASE_URL)
    client = get_client(base_url)
    try:
        health = client.health()
        st.success("Backend connected")
        st.caption(f"OCR engine: **{health.get('ocr_engine') or 'not available'}**")
        if health.get("llm_configured"):
            st.caption(f"LLM explanations: **on** ({health.get('llm_model')})")
        else:
            st.caption("LLM explanations: **off** — rule-based mode")
        st.caption(f"Reference data: `{health.get('reference_data_version')}`")
    except APIError as exc:
        health = None
        st.error(str(exc))
        st.code("uvicorn app.main:app --port 8000", language="bash")
    use_llm = st.toggle("Use LLM explanations when available", value=True)
    st.divider()
    st.markdown(
        '<p class="disclaimer">Assistance tool only. Results are not a certification of GST compliance '
        "and do not replace review by a qualified GST professional.</p>",
        unsafe_allow_html=True,
    )

st.title("GST Invoice Checker Agent")
st.caption("Upload a GST invoice (PDF, PNG or JPG) to extract its details and check GSTINs, HSN/SAC rates and tax calculations.")

tab_check, tab_history = st.tabs(["Check invoice", "History"])


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------
def render_report(report: dict[str, Any], invoice_id: str, key: str = "main") -> None:
    status = report["overall_status"]
    fg, bg, label = STATUS_STYLE.get(status, ("#334155", "#f1f5f9", ""))
    st.markdown(
        f'<div class="status-card" style="background:{bg};color:{fg}"><h3>{status}</h3>'
        f"<p>{label}. {report['overall_explanation']}</p></div>",
        unsafe_allow_html=True,
    )
    s = report["summary"]
    cols = st.columns(5)
    cols[0].metric("Checks run", s["total_checks"])
    cols[1].metric("Passed", s["passed"])
    cols[2].metric("Errors", s["errors"])
    cols[3].metric("Warnings", s["warnings"])
    cols[4].metric("Manual review", s["manual_review"])
    if report.get("llm_note"):
        st.caption(f"ℹ️ {report['llm_note']}")

    issues = [c for c in report["checks"] if c["status"] != "PASS"]
    st.subheader(f"Issues ({len(issues)})")
    if not issues:
        st.success("No issues found by the automated checks.")
    order = {"error": 0, "warning": 1, "info": 2}
    for c in sorted(issues, key=lambda c: order.get(c["severity"], 3)):
        with st.expander(f"{CHECK_ICON.get(c['status'], '')} **{c['name']}** — {c['status'].replace('_', ' ')}",
                         expanded=c["severity"] == "error"):
            st.markdown(f"**Finding:** {c['message']}")
            if c.get("expected") or c.get("actual"):
                st.markdown(f"**Expected:** {c.get('expected') or '—'}  ·  **Found:** {c.get('actual') or '—'}")
            st.markdown(f"**Why it matters:** {c.get('explanation') or '—'}")
            st.markdown(f"**Suggested correction:** {c.get('suggestion') or '—'}")
            if c.get("explanation_source") == "llm":
                st.caption("Explanation written by the AI assistant from the rule engine's finding.")

    st.subheader("All checks")
    df = pd.DataFrame([
        {"Status": f"{CHECK_ICON.get(c['status'], '')} {c['status'].replace('_', ' ')}",
         "Category": c["category"], "Check": c["name"], "Result": c["message"]}
        for c in report["checks"]
    ])
    st.dataframe(df, use_container_width=True, hide_index=True)

    left, right = st.columns(2)
    with left:
        st.subheader("Manual verification")
        for item in report["manual_verification"]:
            st.markdown(f"- {item}")
    with right:
        st.subheader("Assumptions")
        for item in report["assumptions"]:
            st.markdown(f"- {item}")

    with st.expander("Agent workflow trace"):
        for step in report.get("agent_trace", []):
            st.markdown(f"**{step['step']}** — {step['detail']}")

    st.subheader("Download report")
    d1, d2, d3 = st.columns(3)
    d1.download_button("JSON", json.dumps(report, indent=2, ensure_ascii=False), f"gst-report-{invoice_id}.json",
                       "application/json", key=f"dl-{key}-json", use_container_width=True)
    try:
        d2.download_button("Markdown", client.report_text(invoice_id, "markdown"), f"gst-report-{invoice_id}.md",
                           "text/markdown", key=f"dl-{key}-md", use_container_width=True)
        d3.download_button("HTML", client.report_text(invoice_id, "html"), f"gst-report-{invoice_id}.html",
                           "text/html", key=f"dl-{key}-html", use_container_width=True)
    except APIError as exc:
        st.warning(f"Could not prepare Markdown/HTML report: {exc}")
    st.markdown(f'<p class="disclaimer">{report["disclaimer"]}</p>', unsafe_allow_html=True)


def render_extraction(extraction: dict[str, Any]) -> None:
    inv = extraction["invoice"]
    status = extraction["status"]
    method = {"pdf_text": "PDF text layer", "ocr": "OCR", "mixed": "PDF text + OCR"}.get(extraction["method"],
                                                                                         extraction["method"])
    engine = f" ({extraction['ocr_engine']})" if extraction.get("ocr_engine") else ""
    st.caption(f"Extraction: **{status}** via {method}{engine}")
    if extraction.get("error"):
        st.error(extraction["error"])
    for warning in extraction.get("warnings", []):
        st.warning(warning)

    uncertain = set(extraction.get("uncertain_fields", []))
    rows = []
    for key, label in FIELD_LABELS:
        value = inv.get(key)
        flag = "🔍 verify" if key in uncertain else ("missing" if value in (None, "") else "")
        rows.append({"Field": label, "Value": value if value not in (None, "") else "—", "Note": flag})
    for key, label in AMOUNT_LABELS:
        value = inv.get(key)
        flag = "🔍 verify" if key in uncertain else ""
        rows.append({"Field": label, "Value": inr(value), "Note": flag})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    if inv.get("line_items"):
        st.markdown("**Line items**")
        st.dataframe(pd.DataFrame([
            {"Description": li.get("description"), "HSN/SAC": li.get("hsn_sac") or "—",
             "Qty": li.get("quantity"), "Unit price": inr(li.get("unit_price")),
             "Taxable value": inr(li.get("taxable_value")),
             "GST %": f"{li['gst_rate']}%" if li.get("gst_rate") is not None else "—"}
            for li in inv["line_items"]
        ]), use_container_width=True, hide_index=True)
    if extraction.get("missing_fields"):
        st.info("Not found on the invoice: " + ", ".join(f.replace("_", " ") for f in extraction["missing_fields"]))


def corrections_form(invoice_id: str, extraction: dict[str, Any]) -> None:
    inv = extraction["invoice"]
    with st.expander("✏️ Correct extracted data and re-validate"):
        st.caption("Fix OCR mistakes or fill in missing values. Leave a field empty if it is not on the invoice.")
        with st.form(f"corrections-{invoice_id}"):
            values: dict[str, Any] = {}
            c1, c2 = st.columns(2)
            for i, (key, label) in enumerate(FIELD_LABELS):
                target = c1 if i % 2 == 0 else c2
                values[key] = target.text_input(label, value=str(inv.get(key) or ""),
                                                help="Date format: YYYY-MM-DD" if key == "invoice_date" else None)
            a_cols = st.columns(3)
            for i, (key, label) in enumerate(AMOUNT_LABELS):
                values[key] = a_cols[i % 3].text_input(label, value=str(inv.get(key) or ""))
            items = pd.DataFrame(inv.get("line_items") or [],
                                 columns=["description", "hsn_sac", "quantity", "unit", "unit_price",
                                          "taxable_value", "gst_rate"])
            edited = st.data_editor(items, num_rows="dynamic", use_container_width=True, key=f"items-{invoice_id}")
            submitted = st.form_submit_button("Re-validate with corrections", type="primary")
        if submitted:
            payload = {k: (v.strip() or None) for k, v in values.items()}
            line_items = []
            for row in edited.to_dict(orient="records"):
                clean = {k: (None if pd.isna(v) or v == "" else str(v)) for k, v in row.items()}
                if any(clean.values()):
                    line_items.append(clean)
            payload["line_items"] = line_items
            try:
                with st.spinner("Re-validating…"):
                    client.validate(invoice_id, payload, use_llm=use_llm)
                    st.session_state["report"] = client.report(invoice_id)
                    st.session_state["extraction"] = st.session_state["report"]["extraction"]
                st.rerun()
            except APIError as exc:
                st.error(f"Re-validation failed: {exc}")


# ---------------------------------------------------------------------------
# Check tab
# ---------------------------------------------------------------------------
with tab_check:
    uploaded = st.file_uploader("Invoice file", type=["pdf", "png", "jpg", "jpeg"],
                                help="Maximum 10 MB. Text-based PDFs give the most accurate results.")
    if uploaded is not None and health is not None:
        file_key = f"{uploaded.name}-{uploaded.size}"
        if st.session_state.get("file_key") != file_key:
            st.session_state["file_key"] = file_key
            st.session_state["content"] = uploaded.getvalue()
            st.session_state["name"] = uploaded.name
            st.session_state.pop("report", None)
            try:
                with st.spinner("Uploading and extracting invoice details (OCR may take a few seconds)…"):
                    result = client.upload(uploaded.name, uploaded.getvalue(), uploaded.type or "application/octet-stream")
                st.session_state["invoice_id"] = result["invoice_id"]
                st.session_state["extraction"] = result["extraction"]
            except APIError as exc:
                st.session_state.pop("invoice_id", None)
                st.session_state.pop("extraction", None)
                st.error(f"Upload failed: {exc}")

    if st.session_state.get("invoice_id") and st.session_state.get("extraction"):
        invoice_id = st.session_state["invoice_id"]
        preview_col, data_col = st.columns([2, 3])
        with preview_col:
            st.subheader("Invoice preview")
            render_preview(st.session_state["content"], st.session_state["name"])
        with data_col:
            st.subheader("Extracted information")
            render_extraction(st.session_state["extraction"])
            corrections_form(invoice_id, st.session_state["extraction"])
            if st.button("Validate invoice", type="primary", use_container_width=True):
                try:
                    with st.spinner("Running GST checks…"):
                        client.validate(invoice_id, use_llm=use_llm)
                        st.session_state["report"] = client.report(invoice_id)
                except APIError as exc:
                    st.error(f"Validation failed: {exc}")
        if st.session_state.get("report"):
            st.divider()
            st.header("Validation report")
            render_report(st.session_state["report"], invoice_id)
    elif health is not None:
        st.info("Sample invoices are available in `data/sample_invoices/`.")

# ---------------------------------------------------------------------------
# History tab
# ---------------------------------------------------------------------------
with tab_history:
    if health is None:
        st.warning("Connect to the backend to see the processing history.")
    else:
        try:
            history = client.history(100)
        except APIError as exc:
            history = []
            st.error(str(exc))
        if not history:
            st.info("No invoices processed yet.")
        else:
            df = pd.DataFrame([
                {"Uploaded (UTC)": h["uploaded_at"][:19].replace("T", " "), "File": h["original_filename"],
                 "Extraction": h.get("extraction_status") or "—", "Status": h.get("overall_status") or "not validated",
                 "Invoice ID": h["invoice_id"]}
                for h in history
            ])
            st.dataframe(df, use_container_width=True, hide_index=True)
            validated = [h for h in history if h.get("overall_status")]
            if validated:
                choice = st.selectbox(
                    "Open a past report",
                    options=[h["invoice_id"] for h in validated],
                    format_func=lambda i: next(f"{h['original_filename']} — {h['overall_status']} "
                                               f"({h['uploaded_at'][:16].replace('T', ' ')})"
                                               for h in validated if h["invoice_id"] == i),
                )
                if choice:
                    try:
                        render_report(client.report(choice), choice, key="history")
                    except APIError as exc:
                        st.error(str(exc))

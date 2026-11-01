"""End-to-end demo against a running backend.

Usage:
    python scripts/demo_workflow.py [path/to/invoice] [--api http://127.0.0.1:8000]

Steps: upload -> extract -> validate GSTIN/tax -> list errors & warnings -> final report.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from frontend.api_client import GSTApiClient  # noqa: E402

ICON = {"PASS": "[ok]", "FAIL": "[FAIL]", "MISSING": "[MISSING]", "WARNING": "[WARN]", "MANUAL_REVIEW": "[REVIEW]"}
CTYPE = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # rupee symbols on Windows consoles
    parser = argparse.ArgumentParser()
    parser.add_argument("invoice", nargs="?", default=str(ROOT / "data/sample_invoices/invalid_gst_errors.pdf"))
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    client = GSTApiClient(args.api)
    path = Path(args.invoice)

    health = client.health()
    print(f"Backend OK - OCR: {health['ocr_engine']}, LLM configured: {health['llm_configured']}, "
          f"reference data: {health['reference_data_version']}\n")

    print(f"STEP 1 - Upload: {path.name}")
    upload = client.upload(path.name, path.read_bytes(), CTYPE.get(path.suffix.lower(), "application/octet-stream"))
    invoice_id = upload["invoice_id"]
    print(f"  invoice_id = {invoice_id}\n")

    ex = upload["extraction"]
    inv = ex["invoice"]
    print(f"STEP 2 - Extraction: status={ex['status']} method={ex['method']} ocr_engine={ex['ocr_engine']}")
    for key in ("supplier_name", "supplier_gstin", "recipient_name", "recipient_gstin", "invoice_number",
                "invoice_date", "place_of_supply", "taxable_value", "cgst", "sgst", "igst", "round_off",
                "total_invoice_value"):
        print(f"  {key:20} {inv.get(key)}")
    for i, li in enumerate(inv["line_items"], 1):
        print(f"  line {i}: {li['description']} | HSN {li['hsn_sac']} | {li['quantity']} x {li['unit_price']} "
              f"= {li['taxable_value']} @ {li['gst_rate']}%")
    print(f"  missing: {ex['missing_fields'] or 'none'}   uncertain: {ex['uncertain_fields'] or 'none'}\n")

    print("STEP 3 - Validate GSTIN, HSN/SAC rates and tax calculations")
    result = client.validate(invoice_id)
    report = client.report(invoice_id)
    for c in report["checks"]:
        if c["category"] in ("GSTIN", "Tax calculation", "HSN/SAC & rate", "Place of supply"):
            print(f"  {ICON[c['status']]:9} {c['name']}: {c['message']}")
    print()

    print("STEP 4 - Errors and warnings")
    issues = [c for c in report["checks"] if c["status"] != "PASS"]
    if not issues:
        print("  none")
    for c in issues:
        print(f"  {ICON[c['status']]} {c['name']}")
        print(f"      finding:    {c['message']}")
        print(f"      why:        {c['explanation']}")
        print(f"      correction: {c['suggestion']}")
    print()

    print("STEP 5 - Final validation report")
    s = result["summary"]
    print(f"  OVERALL STATUS: {result['overall_status']}")
    print(f"  checks={s['total_checks']} passed={s['passed']} errors={s['errors']} "
          f"warnings={s['warnings']} manual_review={s['manual_review']}")
    print(f"  {report['overall_explanation']}")
    print(f"  explanations: {report['llm_note'] or 'rule-based'}")
    out = ROOT / "reports" / f"demo-{path.stem}.md"
    out.write_text(client.report_text(invoice_id, "markdown"), encoding="utf-8")
    print(f"  Markdown report saved to {out.relative_to(ROOT)}")
    print(f"  JSON report:  {args.api}/invoices/{invoice_id}/report")
    print(f"  HTML report:  {args.api}/invoices/{invoice_id}/report?format=html")


if __name__ == "__main__":
    main()

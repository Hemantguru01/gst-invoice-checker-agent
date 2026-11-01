"""Generate the synthetic sample invoices in data/sample_invoices/.

Usage:  python scripts/generate_sample_invoices.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.utils.sample_invoice import (  # noqa: E402
    SCENARIOS,
    pdf_to_png,
    png_to_scanned_pdf,
    render_pdf,
    valid_intra_state,
)


def main() -> None:
    out_dir = ROOT / "data" / "sample_invoices"
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, factory in SCENARIOS.items():
        path = out_dir / f"{name}.pdf"
        path.write_bytes(render_pdf(factory()))
        print(f"wrote {path.relative_to(ROOT)}")

    pdf = render_pdf(valid_intra_state())
    png = pdf_to_png(pdf, dpi=200)
    (out_dir / "scanned_intra_state.png").write_bytes(png)
    (out_dir / "scanned_intra_state.pdf").write_bytes(png_to_scanned_pdf(png))
    print("wrote data/sample_invoices/scanned_intra_state.png and scanned_intra_state.pdf")


if __name__ == "__main__":
    main()

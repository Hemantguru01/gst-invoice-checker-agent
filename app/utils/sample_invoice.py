"""Renders synthetic GST invoices as PDF/PNG for demos and tests.

All names, PANs and GSTINs are fictitious. GSTINs are built from made-up PANs with
a computed checksum so they are structurally valid but do not belong to anyone.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field

import pymupdf as fitz
from PIL import Image

from app.validators.gstin import compute_checksum


def make_gstin(state_code: str, pan: str, entity: str = "1") -> str:
    body = f"{state_code}{pan}{entity}Z"
    return body + compute_checksum(body)


@dataclass
class SampleLine:
    description: str
    hsn: str | None
    qty: str
    unit: str
    rate: str
    taxable: str
    gst: str | None


@dataclass
class SampleInvoice:
    supplier_name: str
    supplier_address: str
    supplier_gstin: str | None
    supplier_state: str
    invoice_number: str | None
    invoice_date: str | None
    place_of_supply: str | None
    recipient_name: str | None
    recipient_address: str
    recipient_gstin: str | None
    lines: list[SampleLine]
    summary: list[tuple[str, str]] = field(default_factory=list)
    amount_in_words: str = ""
    note: str = ""  # e.g. an export / LUT declaration printed above the footer
    rows_per_page: int | None = None  # split the item table across pages (header repeated)


def render_pdf(inv: SampleInvoice) -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    font, bold = "helv", "hebo"

    def text(x: float, y: float, s: str, size: float = 9, fontname: str = font) -> None:
        page.insert_text((x, y), s, fontsize=size, fontname=fontname)

    def rtext(x_right: float, y: float, s: str, size: float = 9, fontname: str = font) -> None:
        width = fitz.get_text_length(s, fontname=fontname, fontsize=size)
        page.insert_text((x_right - width, y), s, fontsize=size, fontname=fontname)

    def table_header(y: float) -> None:
        page.draw_rect(fitz.Rect(38, y - 12, 557, y + 4), color=(0.6, 0.6, 0.6))
        for x, h in cols:
            text(x, y, h, 9, bold)
        for x, h in rcols:
            rtext(x, y, h, 9, bold)

    text(230, 50, "TAX INVOICE", 16, bold)
    text(40, 66, "SYNTHETIC SAMPLE - NOT A REAL INVOICE", 7)
    page.draw_line((40, 74), (555, 74))

    # Supplier block (left) and invoice meta (right)
    text(40, 92, f"Supplier: {inv.supplier_name}", 10, bold)
    if inv.invoice_number:
        text(360, 92, f"Invoice No: {inv.invoice_number}")
    text(40, 106, inv.supplier_address)
    if inv.invoice_date:
        text(360, 106, f"Invoice Date: {inv.invoice_date}")
    if inv.supplier_gstin:
        text(40, 120, f"GSTIN: {inv.supplier_gstin}")
    if inv.place_of_supply:
        text(360, 120, f"Place of Supply: {inv.place_of_supply}")
    text(40, 134, f"State: {inv.supplier_state}")

    page.draw_line((40, 146), (555, 146))
    text(40, 164, f"Bill To: {inv.recipient_name}" if inv.recipient_name else "Bill To:", 10, bold)
    text(40, 178, inv.recipient_address)
    if inv.recipient_gstin:
        text(40, 192, f"GSTIN: {inv.recipient_gstin}")

    # Item table
    y = 222
    cols = [(40, "S.No"), (70, "Description"), (265, "HSN/SAC"), (320, "Qty"), (350, "Unit")]
    rcols = [(445, "Rate"), (515, "Taxable Value"), (555, "GST %")]
    table_header(y)
    for i, line in enumerate(inv.lines, start=1):
        if inv.rows_per_page and i > 1 and (i - 1) % inv.rows_per_page == 0:
            text(40, y + 22, "Continued on next page", 8)
            text(480, 820, f"Page {doc.page_count}", 7.5)
            page = doc.new_page(width=595, height=842)
            text(40, 50, f"TAX INVOICE (continued)  {inv.invoice_number or ''}", 10, bold)
            y = 80
            table_header(y)
        y += 18
        text(40, y, str(i))
        text(70, y, line.description)
        if line.hsn:
            text(265, y, line.hsn)
        text(320, y, line.qty)
        text(350, y, line.unit)
        rtext(445, y, line.rate)
        rtext(515, y, line.taxable)
        if line.gst:
            rtext(555, y, line.gst)
    y += 10
    page.draw_line((40, y), (555, y))

    # Summary
    for label, value in inv.summary:
        y += 16
        is_total = "total" in label.lower()
        text(330, y, label, 9.5, bold if is_total else font)
        rtext(555, y, value, 9.5, bold if is_total else font)
    if inv.amount_in_words:
        y += 26
        text(40, y, f"Amount in words: {inv.amount_in_words}", 8.5)
    if inv.note:
        y += 20
        text(40, y, inv.note, 8.5)
    text(40, 790, "Declaration: synthetic invoice generated for software testing.", 7.5)
    text(430, 790, "Authorised Signatory", 8.5)
    data = doc.tobytes()
    doc.close()
    return data


def pdf_to_png(pdf: bytes, dpi: int = 200) -> bytes:
    with fitz.open(stream=pdf, filetype="pdf") as doc:
        return doc[0].get_pixmap(dpi=dpi).tobytes("png")


def png_to_scanned_pdf(png: bytes) -> bytes:
    """Image-only PDF (no text layer), like a scanned document. Embedded as JPEG to stay small."""
    buffer = io.BytesIO()
    Image.open(io.BytesIO(png)).convert("RGB").save(buffer, format="JPEG", quality=85)
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_image(page.rect, stream=buffer.getvalue())
    data = doc.tobytes()
    doc.close()
    return data


# ---------------------------------------------------------------------------
# Ready-made synthetic scenarios
# ---------------------------------------------------------------------------
SUPPLIER_KA = make_gstin("29", "AAQCS4821K")  # fictitious PAN
RECIPIENT_KA = make_gstin("29", "AAKFK7390P")
SUPPLIER_MH = make_gstin("27", "AAHCM5512D")
RECIPIENT_GJ = make_gstin("24", "AAGFV3307R")


def _bad_checksum(gstin: str) -> str:
    wrong = "0" if gstin[-1] != "0" else "1"
    return gstin[:-1] + wrong


def valid_intra_state() -> SampleInvoice:
    return SampleInvoice(
        supplier_name="Sahyadri Tech Solutions Pvt Ltd",
        supplier_address="12, 4th Cross, Indiranagar, Bengaluru 560038",
        supplier_gstin=SUPPLIER_KA, supplier_state="Karnataka (29)",
        invoice_number="STS/25-26/0142", invoice_date="15-10-2025", place_of_supply="29-Karnataka",
        recipient_name="Kaveri Retail Traders", recipient_address="88, MG Road, Mysuru 570001",
        recipient_gstin=RECIPIENT_KA,
        lines=[
            SampleLine("Laptop 14 inch 16GB RAM", "8471", "2", "Nos", "45,000.00", "90,000.00", "18%"),
            SampleLine("Software development services", "998314", "40", "Hrs", "1,500.00", "60,000.00", "18%"),
        ],
        summary=[("Taxable Value", "1,50,000.00"), ("CGST @ 9%", "13,500.00"), ("SGST @ 9%", "13,500.00"),
                 ("Total Invoice Value", "1,77,000.00")],
        amount_in_words="Rupees One Lakh Seventy Seven Thousand Only",
    )


def valid_inter_state() -> SampleInvoice:
    return SampleInvoice(
        supplier_name="Mahalaxmi Office Systems LLP",
        supplier_address="Unit 7, Andheri East, Mumbai 400069",
        supplier_gstin=SUPPLIER_MH, supplier_state="Maharashtra (27)",
        invoice_number="MOS/2025/1187", invoice_date="03-11-2025", place_of_supply="24-Gujarat",
        recipient_name="Vadodara Print Hub", recipient_address="Alkapuri, Vadodara 390007",
        recipient_gstin=RECIPIENT_GJ,
        lines=[
            SampleLine("Laser printer A4 duplex", "8443", "3", "Nos", "12,500.00", "37,500.00", "18%"),
            SampleLine("SSD 1TB external", "8523", "10", "Nos", "1,850.50", "18,505.00", "18%"),
        ],
        summary=[("Taxable Value", "56,005.00"), ("IGST @ 18%", "10,080.90"), ("Round Off", "0.10"),
                 ("Total Invoice Value", "66,086.00")],
        amount_in_words="Rupees Sixty Six Thousand Eighty Six Only",
    )


def invalid_invoice() -> SampleInvoice:
    """Several deliberate errors: checksum, line arithmetic, wrong rate, CGST != SGST, wrong total."""
    return SampleInvoice(
        supplier_name="Sahyadri Tech Solutions Pvt Ltd",
        supplier_address="12, 4th Cross, Indiranagar, Bengaluru 560038",
        supplier_gstin=_bad_checksum(SUPPLIER_KA), supplier_state="Karnataka (29)",
        invoice_number="STS/25-26/0199", invoice_date="20-10-2025", place_of_supply="29-Karnataka",
        recipient_name="Kaveri Retail Traders", recipient_address="88, MG Road, Mysuru 570001",
        recipient_gstin=RECIPIENT_KA,
        lines=[
            SampleLine("Laptop 14 inch 16GB RAM", "8471", "2", "Nos", "45,000.00", "95,000.00", "12%"),
            SampleLine("IT support services", "998313", "10", "Hrs", "1,200.00", "12,000.00", "18%"),
        ],
        summary=[("Taxable Value", "1,07,000.00"), ("CGST @ 6%", "6,780.00"), ("SGST @ 6%", "6,700.00"),
                 ("Total Invoice Value", "1,25,000.00")],
    )


def missing_fields_invoice() -> SampleInvoice:
    return SampleInvoice(
        supplier_name="Kaveri Stationery Mart",
        supplier_address="23, Avenue Road, Bengaluru 560002",
        supplier_gstin=make_gstin("29", "ABMPK6621L"), supplier_state="Karnataka (29)",
        invoice_number=None, invoice_date="22-10-2025", place_of_supply=None,
        recipient_name="Walk-in customer", recipient_address="Bengaluru",
        recipient_gstin=None,
        lines=[
            SampleLine("Office chair ergonomic", "9403", "2", "Nos", "6,000.00", "12,000.00", "18%"),
            SampleLine("Desk organiser set", None, "4", "Nos", "500.00", "2,000.00", "18%"),
        ],
        summary=[("Taxable Value", "14,000.00"), ("CGST @ 9%", "1,260.00"), ("SGST @ 9%", "1,260.00"),
                 ("Total Invoice Value", "16,520.00")],
    )


def multi_rate_multipage_invoice() -> SampleInvoice:
    """Two pages, two GST rates (18% and 5%), one CGST/SGST row per rate."""
    return SampleInvoice(
        supplier_name="Sahyadri Tech Solutions Pvt Ltd",
        supplier_address="12, 4th Cross, Indiranagar, Bengaluru 560038",
        supplier_gstin=SUPPLIER_KA, supplier_state="Karnataka (29)",
        invoice_number="STS/25-26/0233", invoice_date="05-11-2025", place_of_supply="29-Karnataka",
        recipient_name="Kaveri Retail Traders", recipient_address="88, MG Road, Mysuru 570001",
        recipient_gstin=RECIPIENT_KA,
        lines=[
            SampleLine("Laptop 14 inch 16GB RAM", "8471", "1", "Nos", "50,000.00", "50,000.00", "18%"),
            SampleLine("UPS 1kVA", "8504", "2", "Nos", "4,000.00", "8,000.00", "18%"),
            SampleLine("Paracetamol 500mg strips", "3004", "100", "Nos", "20.00", "2,000.00", "5%"),
            SampleLine("SSD 1TB external", "8523", "2", "Nos", "3,000.00", "6,000.00", "18%"),
            SampleLine("Cough syrup 100ml", "3004", "50", "Nos", "40.00", "2,000.00", "5%"),
        ],
        summary=[("Taxable Value", "68,000.00"), ("CGST @ 9%", "5,760.00"), ("CGST @ 2.5%", "100.00"),
                 ("SGST @ 9%", "5,760.00"), ("SGST @ 2.5%", "100.00"), ("Total Invoice Value", "79,720.00")],
        rows_per_page=3,
    )


def discount_invoice() -> SampleInvoice:
    """Inter-state, single rate, invoice-level discount shown before the taxable value."""
    return SampleInvoice(
        supplier_name="Mahalaxmi Office Systems LLP",
        supplier_address="Unit 7, Andheri East, Mumbai 400069",
        supplier_gstin=SUPPLIER_MH, supplier_state="Maharashtra (27)",
        invoice_number="MOS/2025/1204", invoice_date="10-11-2025", place_of_supply="24-Gujarat",
        recipient_name="Vadodara Print Hub", recipient_address="Alkapuri, Vadodara 390007",
        recipient_gstin=RECIPIENT_GJ,
        lines=[
            SampleLine("Laser printer A4 duplex", "8443", "2", "Nos", "12,500.00", "25,000.00", "18%"),
            SampleLine("SSD 1TB external", "8523", "5", "Nos", "2,000.00", "10,000.00", "18%"),
        ],
        summary=[("Sub Total", "35,000.00"), ("Less: Discount", "1,000.00"), ("Taxable Value", "34,000.00"),
                 ("IGST @ 18%", "6,120.00"), ("Total Invoice Value", "40,120.00")],
    )


def export_invoice() -> SampleInvoice:
    """Export of services under LUT: zero-rated, no IGST charged. Must not be auto-classified."""
    return SampleInvoice(
        supplier_name="Sahyadri Tech Solutions Pvt Ltd",
        supplier_address="12, 4th Cross, Indiranagar, Bengaluru 560038",
        supplier_gstin=SUPPLIER_KA, supplier_state="Karnataka (29)",
        invoice_number="STS/EXP/0007", invoice_date="12-11-2025", place_of_supply="96-Other Country",
        recipient_name="Northwind Analytics Ltd (Singapore)", recipient_address="1 Raffles Place, Singapore",
        recipient_gstin=None,
        lines=[SampleLine("Software development services", "998314", "100", "Hrs", "2,000.00", "2,00,000.00", "0%")],
        summary=[("Taxable Value", "2,00,000.00"), ("IGST @ 0%", "0.00"), ("Total Invoice Value", "2,00,000.00")],
        note="Supply meant for export under LUT without payment of integrated tax.",
    )


def ocr_confusable_gstin() -> str:
    """Supplier GSTIN with one digit replaced by a look-alike letter (8 -> B), as OCR might read it."""
    return SUPPLIER_KA.replace("4821", "4B21")


SCENARIOS = {
    "valid_intra_state": valid_intra_state,
    "valid_inter_state": valid_inter_state,
    "invalid_gst_errors": invalid_invoice,
    "missing_fields": missing_fields_invoice,
    "multi_rate_multipage": multi_rate_multipage_invoice,
    "discount_inter_state": discount_invoice,
    "export_under_lut": export_invoice,
}

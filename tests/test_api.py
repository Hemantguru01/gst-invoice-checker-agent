import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_invoice_service
from app.main import app
from app.utils.sample_invoice import (
    RECIPIENT_KA,
    invalid_invoice,
    missing_fields_invoice,
    render_pdf,
    valid_intra_state,
)


@pytest.fixture
def client(make_service):
    service = make_service()
    app.dependency_overrides[get_invoice_service] = lambda: service
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def upload(client, name, content, ctype="application/pdf"):
    return client.post("/invoices/upload", files={"file": (name, content, ctype)})


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["llm_configured"] is False
    assert body["reference_data_version"]


def test_full_workflow_valid_invoice(client):
    res = upload(client, "valid.pdf", render_pdf(valid_intra_state()))
    assert res.status_code == 201, res.text
    body = res.json()
    invoice_id = body["invoice_id"]
    assert body["extraction"]["status"] == "success"

    res = client.post(f"/invoices/{invoice_id}/validate")
    assert res.status_code == 200
    assert res.json()["overall_status"] == "VALID"

    record = client.get(f"/invoices/{invoice_id}").json()
    assert record["overall_status"] == "VALID"
    assert record["extraction"]["invoice"]["invoice_number"] == "STS/25-26/0142"

    report = client.get(f"/invoices/{invoice_id}/report").json()
    assert report["summary"]["errors"] == 0
    assert "not a certification" in report["disclaimer"]

    md = client.get(f"/invoices/{invoice_id}/report", params={"format": "markdown"})
    assert md.status_code == 200 and "# GST Invoice Validation Report" in md.text
    html = client.get(f"/invoices/{invoice_id}/report", params={"format": "html"})
    assert html.status_code == 200 and "<html" in html.text

    assert client.get(f"/invoices/{invoice_id}/file").content.startswith(b"%PDF")
    history = client.get("/invoices").json()
    assert history[0]["invoice_id"] == invoice_id


def test_invalid_invoice_reports_errors(client):
    invoice_id = upload(client, "bad.pdf", render_pdf(invalid_invoice())).json()["invoice_id"]
    result = client.post(f"/invoices/{invoice_id}/validate").json()
    assert result["overall_status"] == "INVALID"
    report = client.get(f"/invoices/{invoice_id}/report").json()
    failed = {c["check_id"] for c in report["checks"] if c["status"] == "FAIL"}
    assert {"gstin.supplier.checksum", "hsn.line1.rate", "calc.line1.value",
            "calc.cgst_sgst_equal", "calc.invoice_total"} <= failed


def test_corrections_can_be_submitted(client):
    up = upload(client, "missing.pdf", render_pdf(missing_fields_invoice())).json()
    assert client.post(f"/invoices/{up['invoice_id']}/validate").json()["overall_status"] == "INVALID"

    corrected = up["extraction"]["invoice"]
    corrected.update(invoice_number="KSM/101", place_of_supply="29-Karnataka", recipient_gstin=RECIPIENT_KA)
    corrected["line_items"][1]["hsn_sac"] = "4820"
    res = client.post(f"/invoices/{up['invoice_id']}/validate", json={"invoice": corrected, "use_llm": False})
    assert res.status_code == 200
    report = client.get(f"/invoices/{up['invoice_id']}/report").json()
    assert report["extraction"]["field_sources"]["invoice_number"] == "user"
    # 4820 is a low-confidence reference entry, so it still needs a human check.
    assert report["overall_status"] == "NEEDS REVIEW"


def test_report_before_validation_is_404(client):
    invoice_id = upload(client, "v.pdf", render_pdf(valid_intra_state())).json()["invoice_id"]
    res = client.get(f"/invoices/{invoice_id}/report")
    assert res.status_code == 404
    assert res.json()["error"] == "report_not_found"


def test_unknown_invoice_404(client):
    unknown = "00000000-0000-4000-8000-000000000000"
    assert client.get(f"/invoices/{unknown}").status_code == 404
    assert client.post(f"/invoices/{unknown}/validate").status_code == 404


def test_malformed_invoice_id_is_rejected(client):
    # Only server-generated UUIDs are accepted; anything else never reaches storage.
    for bad in ("does-not-exist", "..%2F..%2Fetc", "1 OR 1=1"):
        assert client.get(f"/invoices/{bad}").status_code in (404, 422)
    assert client.get("/invoices/does-not-exist").status_code == 422


def test_invalid_uploads(client):
    assert upload(client, "malware.exe", b"MZ...", "application/octet-stream").status_code == 415
    res = upload(client, "fake.pdf", b"hello world")
    assert res.status_code == 400 and res.json()["error"] == "invalid_file"
    assert upload(client, "broken.pdf", b"%PDF-1.4 broken").status_code == 400
    assert upload(client, "broken.png", b"\x89PNG\r\n\x1a\n\x00\x00", "image/png").status_code == 400
    assert upload(client, "empty.pdf", b"").status_code == 400


def test_oversized_upload(client, make_service):
    service = make_service()
    service.settings.max_upload_mb = 0.001
    app.dependency_overrides[get_invoice_service] = lambda: service
    res = upload(client, "big.pdf", render_pdf(valid_intra_state()))
    assert res.status_code == 413


def test_upload_without_file_is_422(client):
    res = client.post("/invoices/upload")
    assert res.status_code == 422
    assert res.json()["error"] == "invalid_request"

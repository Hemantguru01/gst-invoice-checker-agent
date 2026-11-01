"""API protections: authentication, rate limiting, body-size limit, headers, retention and deletion."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

import app.main as main_module
from app.api.dependencies import get_invoice_service
from app.core.config import Settings, get_settings
from app.core.exceptions import ServiceBusyError
from app.main import app
from app.utils.sample_invoice import render_pdf, valid_intra_state

KEY = "test-key-not-a-real-secret"


def _client(service, settings: Settings | None = None) -> TestClient:
    app.dependency_overrides[get_invoice_service] = lambda: service
    if settings is not None:
        app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def _upload(client, headers=None):
    return client.post("/invoices/upload", headers=headers or {},
                       files={"file": ("v.pdf", render_pdf(valid_intra_state()), "application/pdf")})


def test_api_key_is_enforced_when_configured(make_service):
    service = make_service()
    settings = Settings(_env_file=None, api_key=SecretStr(KEY))
    with _client(service, settings) as client:
        assert client.get("/health").status_code == 200  # probes stay open
        assert client.get("/invoices").status_code == 401
        assert _upload(client).status_code == 401
        assert client.get("/invoices", headers={"X-API-Key": "wrong"}).status_code == 401
        assert client.get("/invoices", headers={"X-API-Key": KEY}).status_code == 200
        assert _upload(client, {"X-API-Key": KEY}).status_code == 201


def test_api_key_never_appears_in_responses(make_service):
    service = make_service()
    settings = Settings(_env_file=None, api_key=SecretStr(KEY))
    with _client(service, settings) as client:
        body = client.get("/health").text + client.get("/invoices").text
    assert KEY not in body
    assert KEY not in repr(settings)


def test_production_refuses_to_start_without_api_key(monkeypatch):
    monkeypatch.setattr(main_module, "get_settings", lambda: Settings(_env_file=None, environment="production"))
    with pytest.raises(RuntimeError, match="API_KEY"):
        main_module.create_app()


def test_production_hides_interactive_docs(monkeypatch, make_service):
    settings = Settings(_env_file=None, environment="production", api_key=SecretStr(KEY))
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    prod_app = main_module.create_app()
    service = make_service()
    prod_app.dependency_overrides[get_invoice_service] = lambda: service
    with TestClient(prod_app) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_rate_limit_on_expensive_endpoints(make_service):
    service = make_service()
    settings = Settings(_env_file=None, rate_limit_per_minute=2)
    with _client(service, settings) as client:
        assert _upload(client).status_code == 201
        assert _upload(client).status_code == 201
        res = _upload(client)
        assert res.status_code == 429 and res.json()["error"] == "rate_limited"
        assert client.get("/invoices").status_code == 200  # cheap reads are not limited


def test_oversized_body_is_rejected_before_buffering(make_service):
    with _client(make_service()) as client:
        res = client.post("/invoices/upload",
                          files={"file": ("big.pdf", b"%PDF-1.4" + b"0" * (12 * 1024 * 1024), "application/pdf")})
    assert res.status_code == 413
    assert res.json() == {"error": "file_too_large", "detail": "The upload exceeds the 10 MB file size limit."}


def test_extraction_concurrency_limit_returns_busy(make_service):
    service = make_service(max_concurrent_extractions=1, extraction_wait_seconds=0.05)
    service._extraction_slots.acquire()  # simulate an extraction already running
    try:
        with pytest.raises(ServiceBusyError):
            service.upload("v.pdf", render_pdf(valid_intra_state()))
    finally:
        service._extraction_slots.release()


def test_security_headers_and_report_csp(make_service):
    with _client(make_service()) as client:
        invoice_id = _upload(client).json()["invoice_id"]
        client.post(f"/invoices/{invoice_id}/validate")
        res = client.get(f"/invoices/{invoice_id}/report", params={"format": "html"})
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["X-Frame-Options"] == "DENY"
    assert res.headers["Cache-Control"] == "no-store"
    assert "default-src 'none'" in res.headers["Content-Security-Policy"]


def test_delete_removes_file_reports_and_record(make_service):
    service = make_service()
    with _client(service) as client:
        invoice_id = _upload(client).json()["invoice_id"]
        assert client.post(f"/invoices/{invoice_id}/validate").status_code == 200
        stored = Path(service.get(invoice_id).stored_path)
        reports = [service.settings.reports_dir / f"{invoice_id}{ext}" for ext in (".json", ".md")]
        assert stored.exists() and all(p.exists() for p in reports)

        assert client.delete(f"/invoices/{invoice_id}").status_code == 204
        assert not stored.exists() and not any(p.exists() for p in reports)
        assert client.get(f"/invoices/{invoice_id}").status_code == 404
        assert client.delete(f"/invoices/{invoice_id}").status_code == 404


def test_retention_purges_old_invoices(make_service):
    service = make_service(retention_days=7)
    invoice_id, _, _ = service.upload("v.pdf", render_pdf(valid_intra_state()))
    stored = Path(service.get(invoice_id).stored_path)
    assert service.purge_expired(now=datetime.now(timezone.utc) + timedelta(days=1)) == 0
    assert service.purge_expired(now=datetime.now(timezone.utc) + timedelta(days=8)) == 1
    assert not stored.exists()
    assert service.history() == []


def test_retention_zero_keeps_invoices(make_service):
    service = make_service(retention_days=0)
    service.upload("v.pdf", render_pdf(valid_intra_state()))
    assert service.purge_expired(now=datetime.now(timezone.utc) + timedelta(days=3650)) == 0

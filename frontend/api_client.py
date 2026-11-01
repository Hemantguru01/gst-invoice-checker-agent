"""Thin HTTP client for the FastAPI backend."""

from __future__ import annotations

import os
from typing import Any

import requests

DEFAULT_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000")


class APIError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GSTApiClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout: float = 180.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        try:
            response = requests.request(method, f"{self.base_url}{path}", timeout=self.timeout, **kwargs)
        except requests.ConnectionError as exc:
            raise APIError(f"Cannot reach the backend at {self.base_url}. Is the FastAPI server running?") from exc
        except requests.Timeout as exc:
            raise APIError("The backend took too long to respond.") from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise APIError(str(detail), response.status_code)
        return response

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health").json()

    def upload(self, filename: str, content: bytes, content_type: str) -> dict[str, Any]:
        files = {"file": (filename, content, content_type)}
        return self._request("POST", "/invoices/upload", files=files).json()

    def validate(self, invoice_id: str, invoice: dict[str, Any] | None = None, use_llm: bool = True) -> dict[str, Any]:
        body: dict[str, Any] = {"use_llm": use_llm}
        if invoice is not None:
            body["invoice"] = invoice
        return self._request("POST", f"/invoices/{invoice_id}/validate", json=body).json()

    def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        return self._request("GET", f"/invoices/{invoice_id}").json()

    def report(self, invoice_id: str) -> dict[str, Any]:
        return self._request("GET", f"/invoices/{invoice_id}/report").json()

    def report_text(self, invoice_id: str, fmt: str) -> str:
        return self._request("GET", f"/invoices/{invoice_id}/report", params={"format": fmt}).text

    def file(self, invoice_id: str) -> bytes:
        return self._request("GET", f"/invoices/{invoice_id}/file").content

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._request("GET", "/invoices", params={"limit": limit}).json()

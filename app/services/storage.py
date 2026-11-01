"""SQLite persistence for invoice records, extraction results and reports."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.models.invoice import ExtractionResult
from app.models.validation import ValidationReport

_SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    id TEXT PRIMARY KEY,
    original_filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    kind TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    stored_path TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    extraction_status TEXT,
    extraction_json TEXT,
    overall_status TEXT,
    report_json TEXT,
    validated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_invoices_uploaded ON invoices(uploaded_at DESC);
"""


@dataclass
class StoredInvoice:
    id: str
    original_filename: str
    content_type: str
    kind: str
    size_bytes: int
    stored_path: str
    uploaded_at: datetime
    extraction: ExtractionResult | None
    overall_status: str | None
    report: ValidationReport | None
    validated_at: datetime | None
    extraction_status: str | None


class InvoiceRepository:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            with self._lock:
                yield conn
                conn.commit()
        finally:
            conn.close()

    def create(self, invoice_id: str, filename: str, content_type: str, kind: str, size: int,
               stored_path: Path, extraction: ExtractionResult) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO invoices (id, original_filename, content_type, kind, size_bytes, stored_path, "
                "uploaded_at, extraction_status, extraction_json) VALUES (?,?,?,?,?,?,?,?,?)",
                (invoice_id, filename, content_type, kind, size, str(stored_path),
                 datetime.now(timezone.utc).isoformat(), extraction.status.value,
                 extraction.model_dump_json()),
            )

    def save_report(self, invoice_id: str, report: ValidationReport) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE invoices SET overall_status=?, report_json=?, validated_at=?, extraction_json=? WHERE id=?",
                (report.overall_status.value, report.model_dump_json(), report.generated_at.isoformat(),
                 report.extraction.model_dump_json(), invoice_id),
            )

    def get(self, invoice_id: str) -> StoredInvoice | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        return self._to_model(row) if row else None

    def list(self, limit: int = 50) -> list[StoredInvoice]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM invoices ORDER BY uploaded_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._to_model(r, include_payload=False) for r in rows]

    @staticmethod
    def _to_model(row: sqlite3.Row, include_payload: bool = True) -> StoredInvoice:
        return StoredInvoice(
            id=row["id"],
            original_filename=row["original_filename"],
            content_type=row["content_type"],
            kind=row["kind"],
            size_bytes=row["size_bytes"],
            stored_path=row["stored_path"],
            uploaded_at=datetime.fromisoformat(row["uploaded_at"]),
            extraction=ExtractionResult.model_validate_json(row["extraction_json"])
            if include_payload and row["extraction_json"] else None,
            overall_status=row["overall_status"],
            report=ValidationReport.model_validate_json(row["report_json"])
            if include_payload and row["report_json"] else None,
            validated_at=datetime.fromisoformat(row["validated_at"]) if row["validated_at"] else None,
            extraction_status=row["extraction_status"],
        )

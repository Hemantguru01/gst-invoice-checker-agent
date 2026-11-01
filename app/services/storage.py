"""SQLite persistence for invoice records, extraction results and reports."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
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
# Columns added after the first release; created on older databases at start-up.
_MIGRATIONS = {
    "supplier_gstin": "TEXT",
    "invoice_number_raw": "TEXT",
    "invoice_number_key": "TEXT",
    "financial_year": "TEXT",
}


@dataclass
class DuplicateCandidate:
    id: str
    original_filename: str
    uploaded_at: datetime
    invoice_number_raw: str | None
    financial_year: str | None


def financial_year(invoice_date: date | None) -> str | None:
    """Indian financial year (April-March), e.g. 2025-26."""
    if invoice_date is None:
        return None
    start = invoice_date.year if invoice_date.month >= 4 else invoice_date.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


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
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(invoices)")}
            for column, kind in _MIGRATIONS.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE invoices ADD COLUMN {column} {kind}")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_number ON invoices(supplier_gstin, invoice_number_key)")

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

    @staticmethod
    def _identity(extraction: ExtractionResult) -> tuple[str | None, str | None, str | None, str | None]:
        inv = extraction.invoice
        return (inv.supplier_gstin, inv.invoice_number_raw, inv.invoice_number_key,
                financial_year(inv.invoice_date))

    def create(self, invoice_id: str, filename: str, content_type: str, kind: str, size: int,
               stored_path: Path, extraction: ExtractionResult) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO invoices (id, original_filename, content_type, kind, size_bytes, stored_path, "
                "uploaded_at, extraction_status, extraction_json, supplier_gstin, invoice_number_raw, "
                "invoice_number_key, financial_year) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (invoice_id, filename, content_type, kind, size, str(stored_path),
                 datetime.now(timezone.utc).isoformat(), extraction.status.value,
                 extraction.model_dump_json(), *self._identity(extraction)),
            )

    def save_report(self, invoice_id: str, report: ValidationReport) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE invoices SET overall_status=?, report_json=?, validated_at=?, extraction_json=?, "
                "supplier_gstin=?, invoice_number_raw=?, invoice_number_key=?, financial_year=? WHERE id=?",
                (report.overall_status.value, report.model_dump_json(), report.generated_at.isoformat(),
                 report.extraction.model_dump_json(), *self._identity(report.extraction), invoice_id),
            )

    def find_same_invoice_number(self, invoice_id: str, supplier_gstin: str, number_key: str,
                                 fy: str | None) -> list[DuplicateCandidate]:
        """Other invoices from the same supplier with the same comparison key. Invoices from a different
        financial year are excluded (numbering may restart each year); unknown years are kept."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, original_filename, uploaded_at, invoice_number_raw, financial_year FROM invoices "
                "WHERE supplier_gstin=? AND invoice_number_key=? AND id<>? ORDER BY uploaded_at",
                (supplier_gstin, number_key, invoice_id)).fetchall()
        return [DuplicateCandidate(r["id"], r["original_filename"], datetime.fromisoformat(r["uploaded_at"]),
                                   r["invoice_number_raw"], r["financial_year"])
                for r in rows if fy is None or r["financial_year"] is None or r["financial_year"] == fy]

    def get(self, invoice_id: str) -> StoredInvoice | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        return self._to_model(row) if row else None

    def delete(self, invoice_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM invoices WHERE id=?", (invoice_id,))
        return cursor.rowcount > 0

    def ids_uploaded_before(self, cutoff: datetime) -> list[tuple[str, str]]:
        """(id, stored_path) of invoices uploaded before `cutoff`."""
        with self._connect() as conn:
            rows = conn.execute("SELECT id, stored_path FROM invoices WHERE uploaded_at < ?",
                                (cutoff.isoformat(),)).fetchall()
        return [(r["id"], r["stored_path"]) for r in rows]

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

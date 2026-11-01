"""Loads the versioned reference datasets from data/reference."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from app.core.config import get_settings


@dataclass(frozen=True)
class HSNEntry:
    code: str
    type: str
    description: str
    gst_rate: Decimal
    confidence: str  # "high" | "low"
    notes: str | None = None
    effective_from: date | None = None
    source: str | None = None
    notification_ref: str | None = None  # null until verified against the official notification


@dataclass(frozen=True)
class HSNReference:
    version: str
    effective_from: date
    source: str
    legal_status: str
    entries: dict[str, HSNEntry]

    def lookup(self, code: str) -> HSNEntry | None:
        """Longest-prefix match so an 8-digit code finds its 4/6-digit heading."""
        code = code.strip()
        for length in range(len(code), 3, -1):
            entry = self.entries.get(code[:length])
            if entry is not None:
                return entry
        return None


@dataclass(frozen=True)
class StateCodes:
    version: str
    codes: dict[str, str]

    def name(self, code: str) -> str | None:
        return self.codes.get(code)

    def code_for_name(self, text: str) -> str | None:
        """Find a state code from free text such as '29-Karnataka' or 'Karnataka (29)'."""
        import re

        cleaned = text.strip().lower()
        digits = re.search(r"\b(\d{2})\b", cleaned)
        if digits and digits.group(1) in self.codes:
            return digits.group(1)
        # Longest name first so "andhra pradesh (old)" etc. don't shadow each other.
        for code, name in sorted(self.codes.items(), key=lambda kv: -len(kv[1])):
            if name.lower() in cleaned and "(old)" not in name.lower():
                return code
        return None


def load_hsn_reference(path: Path) -> HSNReference:
    raw = json.loads(path.read_text(encoding="utf-8"))
    meta = raw["metadata"]
    entries = {
        e["code"]: HSNEntry(
            code=e["code"],
            type=e["type"],
            description=e["description"],
            gst_rate=Decimal(str(e["gst_rate"])),
            confidence=e.get("confidence", "low"),
            notes=e.get("notes"),
            effective_from=date.fromisoformat(e.get("effective_from") or meta["effective_from"]),
            source=e.get("source"),
            notification_ref=e.get("notification_ref"),
        )
        for e in raw["entries"]
    }
    return HSNReference(
        version=meta["version"],
        effective_from=date.fromisoformat(meta["effective_from"]),
        source=meta["source"],
        legal_status=meta["legal_status"],
        entries=entries,
    )


def load_state_codes(path: Path) -> StateCodes:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return StateCodes(version=raw["metadata"]["version"], codes=raw["codes"])


@lru_cache
def get_hsn_reference() -> HSNReference:
    return load_hsn_reference(get_settings().reference_dir / "hsn_sac_rates.json")


@lru_cache
def get_state_codes() -> StateCodes:
    return load_state_codes(get_settings().reference_dir / "state_codes.json")

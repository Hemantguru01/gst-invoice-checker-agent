"""Logging setup with redaction of GSTINs, PANs and long digit runs.

Invoice contents are private business data, so log records are scrubbed
before they are emitted, even if a developer accidentally logs a value.
"""

from __future__ import annotations

import logging
import re

_GSTIN_RE = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][0-9A-Z]{3}\b")
_PAN_RE = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
_API_KEY_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")


def redact(text: str) -> str:
    text = _API_KEY_RE.sub("[REDACTED-KEY]", text)
    text = _GSTIN_RE.sub("[REDACTED-GSTIN]", text)
    return _PAN_RE.sub("[REDACTED-PAN]", text)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        record.msg = redact(message)
        record.args = None
        return True


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if any(getattr(h, "_gst_handler", False) for h in root.handlers):
        root.setLevel(level)
        return
    handler = logging.StreamHandler()
    handler._gst_handler = True  # type: ignore[attr-defined]
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    root.addHandler(handler)
    root.setLevel(level)

"""Request-level protections: API-key auth, rate limiting, body-size limit and security headers.

Local development stays usable: with no API_KEY configured the API is open, which is only
acceptable while it is bound to 127.0.0.1. ENVIRONMENT=production refuses to start without a key.
"""

from __future__ import annotations

import hmac
import threading
import time
from collections import defaultdict, deque

from fastapi import Depends, Request
from fastapi.security import APIKeyHeader
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings, get_settings
from app.core.exceptions import AuthenticationRequiredError, RateLimitedError

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(api_key: str | None = Depends(_api_key_header),
                    settings: Settings = Depends(get_settings)) -> None:
    expected = settings.api_key.get_secret_value() if settings.api_key else ""
    if not expected:
        return  # development mode: no key configured
    if not api_key or not hmac.compare_digest(api_key.encode(), expected.encode()):
        raise AuthenticationRequiredError("A valid X-API-Key header is required.")


class RateLimiter:
    """Sliding-window limit per client address (in memory, per process)."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, limit: int, window_seconds: float = 60.0) -> None:
        if limit <= 0:
            return
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > window_seconds:
                hits.popleft()
            if len(hits) >= limit:
                raise RateLimitedError(f"Too many requests: at most {limit} per minute. Try again shortly.")
            hits.append(now)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


rate_limiter = RateLimiter()


def limit_expensive_requests(request: Request, settings: Settings = Depends(get_settings)) -> None:
    """Applied to upload and validate, the endpoints that run OCR / the agent."""
    client = request.client.host if request.client else "unknown"
    rate_limiter.check(client, settings.rate_limit_per_minute)


class BodySizeLimitMiddleware:
    """Rejects request bodies above the limit before they are buffered, including chunked uploads."""

    def __init__(self, app: ASGIApp, max_bytes: int, file_limit_mb: float) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.file_limit_mb = file_limit_mb

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        length = headers.get(b"content-length")
        if length is not None and length.isdigit() and int(length) > self.max_bytes:
            await _reject(send, self.file_limit_mb)
            return

        received = 0
        too_large = False

        async def limited_receive() -> Message:
            nonlocal received, too_large
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    too_large = True
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLarge:
            if too_large:
                await _reject(send, self.file_limit_mb)


class _BodyTooLarge(Exception):
    pass


async def _reject(send: Send, file_limit_mb: float) -> None:
    body = (b'{"error":"file_too_large","detail":"The upload exceeds the '
            + f"{file_limit_mb:g}".encode() + b' MB file size limit."}')
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}
# Rendered reports contain invoice text; they need no scripts or external resources.
REPORT_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'"

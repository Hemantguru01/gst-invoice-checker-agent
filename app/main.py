"""FastAPI application entry point.

Run with:  uvicorn app.main:app --reload
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.core.config import get_settings
from app.core.exceptions import AppError
from app.core.logging import configure_logging
from app.core.security import SECURITY_HEADERS, BodySizeLimitMiddleware

logger = logging.getLogger(__name__)

DESCRIPTION = """
Validates Indian GST invoices (PDF, PNG, JPG): OCR extraction, GSTIN format/state/checksum checks,
HSN/SAC rate lookup against a versioned reference dataset, tax arithmetic, and an agent that explains
findings (optionally with an LLM). **Assistance tool only — not a GST compliance certification.**
"""


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    settings.ensure_directories()
    if settings.is_production and not (settings.api_key and settings.api_key.get_secret_value().strip()):
        raise RuntimeError("ENVIRONMENT=production requires API_KEY to be set; refusing to start an "
                           "unauthenticated API that stores private invoices.")
    if not settings.api_key:
        logger.warning("No API_KEY configured: the API is unauthenticated. Keep it bound to 127.0.0.1.")

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        from app.api.dependencies import get_invoice_service

        # Honour dependency overrides so tests never touch the real data folder.
        service_factory = application.dependency_overrides.get(get_invoice_service, get_invoice_service)
        removed = service_factory().purge_expired()
        if removed:
            logger.info("Retention: removed %d invoice(s) older than %d day(s).", removed, settings.retention_days)
        yield

    docs = settings.docs_enabled
    app = FastAPI(title=settings.app_name, version=settings.app_version, description=DESCRIPTION,
                  docs_url="/docs" if docs else None, redoc_url="/redoc" if docs else None,
                  openapi_url="/openapi.json" if docs else None, lifespan=lifespan)
    # Multipart overhead on top of the file limit; larger bodies are refused before buffering.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_upload_bytes + 1024 * 1024,
                       file_limit_mb=settings.max_upload_mb)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if request.url.path.startswith("/invoices"):
            response.headers.setdefault("Cache-Control", "no-store")  # invoice data must not be cached
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.code, "detail": exc.message})

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Do not echo submitted values back (they may contain invoice data).
        fields = sorted({".".join(str(p) for p in e.get("loc", [])) for e in exc.errors()})
        return JSONResponse(status_code=422, content={"error": "invalid_request",
                                                      "detail": f"Invalid or missing fields: {', '.join(fields)}"})

    @app.exception_handler(Exception)
    async def unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled error: %s", type(exc).__name__)
        return JSONResponse(status_code=500, content={"error": "internal_error",
                                                      "detail": "An unexpected error occurred."})

    app.include_router(router)
    return app


app = create_app()

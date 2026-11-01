"""FastAPI application entry point.

Run with:  uvicorn app.main:app --reload
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.core.config import get_settings
from app.core.exceptions import AppError
from app.core.logging import configure_logging

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
    app = FastAPI(title=settings.app_name, version=settings.app_version, description=DESCRIPTION)

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

"""Domain exceptions mapped to HTTP responses in app.main."""

from __future__ import annotations


class AppError(Exception):
    status_code = 400
    code = "app_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidFileError(AppError):
    status_code = 400
    code = "invalid_file"


class FileTooLargeError(AppError):
    status_code = 413
    code = "file_too_large"


class UnsupportedFileTypeError(AppError):
    status_code = 415
    code = "unsupported_file_type"


class ExtractionError(AppError):
    status_code = 422
    code = "extraction_failed"


class OCRUnavailableError(ExtractionError):
    code = "ocr_unavailable"


class InvoiceNotFoundError(AppError):
    status_code = 404
    code = "invoice_not_found"


class ReportNotFoundError(AppError):
    status_code = 404
    code = "report_not_found"


class AuthenticationRequiredError(AppError):
    status_code = 401
    code = "authentication_required"


class RateLimitedError(AppError):
    status_code = 429
    code = "rate_limited"


class ServiceBusyError(AppError):
    status_code = 503
    code = "service_busy"


class LLMError(Exception):
    """Raised by LLM clients; always caught by the agent and replaced by the fallback."""

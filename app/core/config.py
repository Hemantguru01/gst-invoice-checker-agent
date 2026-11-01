"""Application settings loaded from environment variables / .env file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "GST Invoice Checker Agent"
    app_version: str = "1.0.0"
    log_level: str = "INFO"

    # Storage
    data_dir: Path = PROJECT_ROOT / "data"
    upload_dir: Path = PROJECT_ROOT / "data" / "uploads"
    reports_dir: Path = PROJECT_ROOT / "reports"
    database_path: Path = PROJECT_ROOT / "data" / "invoices.db"
    reference_dir: Path = PROJECT_ROOT / "data" / "reference"

    # Uploads
    max_upload_mb: float = Field(default=10.0, gt=0, le=50)
    max_pdf_pages: int = Field(default=10, ge=1, le=50)

    # OCR: "auto" tries RapidOCR first, then Tesseract.
    ocr_engine: str = "auto"
    ocr_render_dpi: int = Field(default=200, ge=72, le=400)
    tesseract_cmd: str | None = None

    # LLM (optional). Without a key the rule-based engine runs alone.
    llm_provider: str = "anthropic"  # "anthropic" or "none"
    anthropic_api_key: SecretStr | None = None
    llm_model: str = "claude-opus-5-5"
    llm_timeout_seconds: float = 60.0
    llm_assisted_extraction: bool = True

    # Calculation tolerance in rupees for rounding differences.
    rounding_tolerance: float = Field(default=1.0, ge=0)

    @property
    def max_upload_bytes(self) -> int:
        return int(self.max_upload_mb * 1024 * 1024)

    @property
    def llm_configured(self) -> bool:
        return (
            self.llm_provider.lower() == "anthropic"
            and self.anthropic_api_key is not None
            and bool(self.anthropic_api_key.get_secret_value().strip())
        )

    def ensure_directories(self) -> None:
        for path in (self.data_dir, self.upload_dir, self.reports_dir, self.database_path.parent):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()

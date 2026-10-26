"""Application settings, loaded from environment variables / .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from auditor.exceptions import ConfigurationError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLES_DIR = PROJECT_ROOT / "samples"
REPORTS_DIR = PROJECT_ROOT / "reports"

DEFAULT_MODEL = "claude-sonnet-5-5"

# Native-PDF limits for the Claude API (used only for scanned documents).
NATIVE_PDF_MAX_PAGES = 100
NATIVE_PDF_MAX_BYTES = 32 * 1024 * 1024

load_dotenv(PROJECT_ROOT / ".env")


def _int_env(name: str, default: int, minimum: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"La variable {name} debe ser un número entero (valor actual: {raw!r}).") from exc
    if value < minimum:
        raise ConfigurationError(f"La variable {name} debe ser ≥ {minimum} (valor actual: {value}).")
    return value


@dataclass(frozen=True)
class Settings:
    api_key: str | None
    model: str
    max_output_tokens: int
    max_input_chars: int
    max_upload_mb: int
    request_timeout_s: int

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    def require_api_key(self) -> str:
        if not self.api_key:
            raise ConfigurationError(
                "No se encontró ANTHROPIC_API_KEY. Crea un archivo .env a partir de .env.example "
                "y agrega tu clave de la API de Claude."
            )
        return self.api_key


def load_settings() -> Settings:
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip() or None
    return Settings(
        api_key=api_key,
        model=os.getenv("LEGALSITE_MODEL", "").strip() or DEFAULT_MODEL,
        max_output_tokens=_int_env("LEGALSITE_MAX_OUTPUT_TOKENS", 8000, 1000),
        max_input_chars=_int_env("LEGALSITE_MAX_INPUT_CHARS", 500_000, 10_000),
        max_upload_mb=_int_env("LEGALSITE_MAX_UPLOAD_MB", 50, 1),
        request_timeout_s=_int_env("LEGALSITE_REQUEST_TIMEOUT_S", 300, 30),
    )

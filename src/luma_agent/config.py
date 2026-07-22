"""Central configuration for the Luma Bistro voice agent.

Everything is driven by environment variables (loaded from a local .env if
present) so the same code runs locally, in Docker, or in CI without edits.
Providers are swappable — nothing downstream hard-codes a vendor.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

try:
    # Optional: load a local .env for developer convenience.
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional at runtime
    pass


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _get_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# Fixed restaurant facts — the single source of truth
# the agent is allowed to state without an API call).
RESTAURANT_NAME = "Luma Bistro"
RESTAURANT_TIMEZONE = "America/Los_Angeles"
RESTAURANT_HOURS = "Tuesday to Sunday, 5:00 PM to 10:00 PM; closed Monday"
SLOT_MINUTES = 30
MAX_STANDARD_PARTY_SIZE = 8
# Valid booking times on the 30-minute grid the mock API exposes.
VALID_SLOT_TIMES = ["17:30", "18:00", "18:30", "19:00", "19:30", "20:00"]


@dataclass
class Settings:
    # --- Reservation API ---------------------------------------------------
    reservation_api_url: str = field(
        default_factory=lambda: _get("RESERVATION_API_URL", "http://localhost:8000")
    )
    api_timeout_s: float = field(default_factory=lambda: _get_float("API_TIMEOUT_S", 8.0))
    # Retries for transient upstream failures (HTTP 503 / network errors).
    # 1 == "retry at most once", which is exactly what test T6 asks for.
    api_max_retries: int = field(default_factory=lambda: _get_int("API_MAX_RETRIES", 1))
    api_retry_backoff_ms: int = field(
        default_factory=lambda: _get_int("API_RETRY_BACKOFF_MS", 250)
    )

    # --- LLM ---------------------------------------------------------------
    llm_provider: str = field(default_factory=lambda: _get("LLM_PROVIDER", "openai"))
    openai_api_key: str = field(default_factory=lambda: _get("OPENAI_API_KEY"))
    openai_model: str = field(default_factory=lambda: _get("OPENAI_MODEL", "gpt-4o"))

    # --- STT ---------------------------------------------------------------
    stt_provider: str = field(default_factory=lambda: _get("STT_PROVIDER", "deepgram"))
    deepgram_api_key: str = field(default_factory=lambda: _get("DEEPGRAM_API_KEY"))
    deepgram_model: str = field(default_factory=lambda: _get("DEEPGRAM_MODEL", "nova-2"))

    # --- TTS ---------------------------------------------------------------
    tts_provider: str = field(default_factory=lambda: _get("TTS_PROVIDER", "cartesia"))
    cartesia_api_key: str = field(default_factory=lambda: _get("CARTESIA_API_KEY"))
    # Default Cartesia "British Lady" voice; override per taste.
    cartesia_voice_id: str = field(
        default_factory=lambda: _get(
            "CARTESIA_VOICE_ID", "79a125e8-cd45-4c13-8a67-188112f4dd22"
        )
    )

    # --- Server ------------------------------------------------------------
    host: str = field(default_factory=lambda: _get("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _get_int("PORT", 7860))

    # --- Observability -----------------------------------------------------
    log_level: str = field(default_factory=lambda: _get("LOG_LEVEL", "INFO"))
    log_file: str = field(default_factory=lambda: _get("LOG_FILE", ""))

    # --- Restaurant facts (mirrors constants above; handy on the object) ---
    restaurant_name: str = RESTAURANT_NAME
    timezone: str = RESTAURANT_TIMEZONE
    hours: str = RESTAURANT_HOURS
    slot_minutes: int = SLOT_MINUTES
    max_standard_party_size: int = MAX_STANDARD_PARTY_SIZE

    def require_voice_keys(self) -> list[str]:
        """Return the list of missing keys needed to run the live voice stack."""
        missing = []
        if self.llm_provider == "openai" and not self.openai_api_key:
            missing.append("OPENAI_API_KEY")
        if self.stt_provider == "deepgram" and not self.deepgram_api_key:
            missing.append("DEEPGRAM_API_KEY")
        if self.tts_provider == "cartesia" and not self.cartesia_api_key:
            missing.append("CARTESIA_API_KEY")
        return missing


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

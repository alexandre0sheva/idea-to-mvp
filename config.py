from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Provider = Literal["openai", "anthropic", "google"]


class Settings(BaseSettings):
    """Environment-driven configuration for orchestrator providers and models."""

    _MODULE_DIR = Path(__file__).resolve().parent
    model_config = SettingsConfigDict(
        env_file=(
            ".env",
            str(_MODULE_DIR / ".env"),
        ),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    openai_api_key: str | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    anthropic_api_key: str | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    google_api_key: str | None = Field(default=None, validation_alias="GOOGLE_API_KEY")

    pm_provider: Provider = "openai"
    tech_lead_provider: Provider = "anthropic"
    skeptic_provider: Provider = "google"
    summarizer_provider: Provider = "anthropic"
    architect_provider: Provider = "anthropic"

    pm_model: str = "gpt-5.4-mini"
    openai_fallback_model: str = "gpt-4o-mini"
    tech_lead_model: str = "claude-haiku-4-5-20251001"
    skeptic_model: str = "gemini-3-flash-preview"
    summarizer_model: str = "claude-4-6-sonnet-latest"
    architect_model: str = "claude-4-6-sonnet-latest"

    temperature: float = 0.7
    discussion_max_tokens: int = 900
    skeptic_max_tokens: int = 1200
    summary_max_tokens: int = 2000
    default_rounds: int = 3
    enable_checkpointer: bool = True

    # Implementation stage (Claude Agent SDK). Requires ANTHROPIC_API_KEY (or a
    # logged-in Claude Code install) and spends real tokens; every run is gated
    # behind explicit user approval in the UI.
    implementer_model: str = "claude-opus-4-8"
    implementer_permission_mode: str = "bypassPermissions"
    implementer_max_turns: int = 120
    implementer_max_budget_usd: float = 10.0
    verifier_max_turns: int = 40
    max_fix_attempts: int = 2


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache() -> None:
    get_settings.cache_clear()

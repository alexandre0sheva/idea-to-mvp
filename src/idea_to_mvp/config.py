from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Provider = Literal["openai", "anthropic", "google"]
PermissionMode = Literal["default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"]


class Settings(BaseSettings):
    """Environment-driven configuration for orchestrator providers and models."""

    model_config = SettingsConfigDict(
        env_file=".env",  # read from the current working directory
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    openai_api_key: str | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    anthropic_api_key: str | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    google_api_key: str | None = Field(default=None, validation_alias="GOOGLE_API_KEY")

    # Root for everything the app writes. Deliberately outside the repository so implementation
    # agents never run next to `.env`.
    output_dir: Path = Path("~/idea-to-mvp")

    # Where graph checkpoints live. `sqlite` (default) makes sessions survive restarts and lets an
    # interrupted run resume; `memory` keeps them only for the life of the process.
    checkpointer: Literal["sqlite", "memory"] = "sqlite"
    checkpoint_db: Path | None = None  # default: <output_dir>/sessions.db

    # Offline demo: canned model outputs and a stand-in implementation stage; no API keys needed.
    demo_mode: bool = False

    pm_provider: Provider = "openai"
    tech_lead_provider: Provider = "anthropic"
    skeptic_provider: Provider = "google"
    summarizer_provider: Provider = "anthropic"
    architect_provider: Provider = "anthropic"

    pm_model: str = "gpt-5.6-terra"
    tech_lead_model: str = "claude-sonnet-5-5"
    skeptic_model: str = "gemini-3.8-flash"
    summarizer_model: str = "claude-sonnet-5-5"
    architect_model: str = "claude-sonnet-5-5"

    # Applied only to models that accept sampling parameters (see agents._sampling_kwargs).
    temperature: float | None = 0.7
    # Per-request limits handed to the provider SDKs (which retry 429/5xx/connection errors themselves).
    llm_timeout_seconds: float = 120.0
    llm_max_retries: int = 2
    discussion_max_tokens: int = 900
    skeptic_max_tokens: int = 1200
    summary_max_tokens: int = 2000
    default_rounds: int = 3

    # Implementation stage (Claude Agent SDK). Requires ANTHROPIC_API_KEY (or a
    # logged-in Claude Code install) and spends real tokens; every run is gated
    # behind explicit user approval in the UI.
    implementer_model: str = "claude-opus-5-5"
    implementer_permission_mode: PermissionMode = "bypassPermissions"
    implementer_max_turns: int = 120
    implementer_max_budget_usd: float = 10.0
    verifier_max_turns: int = 40
    max_fix_attempts: int = 2

    @field_validator("output_dir", mode="after")
    @classmethod
    def _expand_output_dir(cls, value: Path) -> Path:
        return value.expanduser()

    @property
    def checkpoint_path(self) -> Path:
        return (self.checkpoint_db or self.output_dir / "sessions.db").expanduser()

    @property
    def exports_dir(self) -> Path:
        return self.output_dir / "exports"

    @property
    def blueprints_dir(self) -> Path:
        return self.output_dir / "blueprints"

    @property
    def projects_dir(self) -> Path:
        return self.output_dir / "projects"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache() -> None:
    get_settings.cache_clear()

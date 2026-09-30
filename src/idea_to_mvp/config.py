from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Provider = Literal["openai", "anthropic", "google"]
PanelMode = Literal["moderated", "round_robin"]
SandboxMode = Literal["auto", "on", "off"]
DEFAULT_ALLOWED_DOMAINS = ("pypi.org", "files.pythonhosted.org", "registry.npmjs.org", "github.com")
PermissionMode = Literal["default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"]


class Settings(BaseSettings):
    """Environment-driven configuration for orchestrator providers and models."""

    model_config = SettingsConfigDict(
        env_file=".env",  # read from the current working directory
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
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
    moderator_provider: Provider = "anthropic"

    pm_model: str = "gpt-5.6-terra"
    tech_lead_model: str = "claude-sonnet-5-5"
    skeptic_model: str = "gemini-3.8-flash"
    summarizer_model: str = "claude-sonnet-5-5"
    architect_model: str = "claude-sonnet-5-5"
    moderator_model: str = "claude-haiku-4-5-20251001"  # a cheap call after every panel turn

    # Applied only to models that accept sampling parameters (see agents._sampling_kwargs).
    temperature: float | None = 0.7
    # Per-request limits handed to the provider SDKs (which retry 429/5xx/connection errors themselves).
    llm_timeout_seconds: float = 120.0
    llm_max_retries: int = 2
    # Upper bound on model calls in flight at once (the panel's opening statements run in parallel).
    llm_max_concurrency: int = Field(default=4, ge=1)
    discussion_max_tokens: int = 900
    skeptic_max_tokens: int = 1200
    summary_max_tokens: int = 2000
    plan_max_tokens: int = 6000  # the structured plan is far longer than one prose document
    default_rounds: int = 3
    # Extra passes the blueprint critic may trigger: blocker-flagged documents are regenerated with the
    # issues appended. 0 = review and report only.
    max_blueprint_revisions: int = Field(default=1, ge=0)
    # moderated: parallel openings, then a moderator picks speakers and may end the debate early.
    # round_robin: the classic fixed PM -> Tech Lead -> Skeptic rotation for the full turn budget.
    panel_mode: PanelMode = "moderated"

    # Implementation stage (Claude Agent SDK). Requires ANTHROPIC_API_KEY (or a
    # logged-in Claude Code install) and spends real tokens; every run is gated
    # behind explicit user approval in the UI.
    implementer_model: str = "claude-opus-5-5"
    # acceptEdits: file edits are auto-approved and, with the OS sandbox on, so are shell commands (they
    # cannot leave the workspace). bypassPermissions approves every tool unattended; the implement gate
    # shows a red warning for it.
    implementer_permission_mode: PermissionMode = "acceptEdits"
    # OS-level sandbox for the agents' shell commands (Seatbelt on macOS, bubblewrap on Linux).
    # auto: on where supported, otherwise a loud warning and the tool-call guard only; on: refuse to run
    # without it; off: no sandbox.
    implementer_sandbox: SandboxMode = "auto"
    # The only hosts sandboxed shell commands may reach (package registries). Comma-separated in .env.
    implementer_allowed_domains: Annotated[list[str], NoDecode] = list(DEFAULT_ALLOWED_DOMAINS)
    implementer_max_turns: int = 120
    # Cost caps. The whole-run cap is shared by all implementation sessions (and limits the verification
    # sessions that follow); the per-task cap bounds one session. IMPLEMENTER_MAX_BUDGET_USD is the old
    # name of the per-task cap and still works.
    implementer_max_total_usd: float = Field(default=25.0, gt=0)
    implementer_max_task_usd: float = Field(
        default=5.0, gt=0, validation_alias=AliasChoices("IMPLEMENTER_MAX_TASK_USD", "IMPLEMENTER_MAX_BUDGET_USD")
    )
    implementer_max_task_turns: int = Field(default=40, gt=0)
    # Independent plan tasks run concurrently, each in its own git worktree and merged back afterwards.
    # 1 = one task at a time, directly in the workspace.
    implementer_max_parallel: int = Field(default=3, ge=1)
    verifier_max_turns: int = 40  # per verification lane
    # Fix-and-reverify rounds a failed verification gets before the delivery report.
    max_fix_attempts: int = 2
    # How many versions a project may go through: after the first delivery the user can request changes, and
    # each round builds the next version (v0.2, ...) in the same workspace. 1 = no iteration gate at all.
    max_iterations: int = Field(default=5, ge=1)

    @field_validator("implementer_allowed_domains", mode="before")
    @classmethod
    def _split_domains(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

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

    @property
    def deliveries_dir(self) -> Path:
        return self.output_dir / "deliveries"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache() -> None:
    get_settings.cache_clear()

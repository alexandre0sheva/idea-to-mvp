from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, get_args

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from idea_to_mvp.model_catalog import Provider, get_catalog

PanelMode = Literal["moderated", "round_robin"]
ModelProfile = Literal["fast", "balanced", "quality"]
SandboxMode = Literal["auto", "on", "off"]
DEFAULT_ALLOWED_DOMAINS = ("pypi.org", "files.pythonhosted.org", "registry.npmjs.org", "github.com")
PermissionMode = Literal["default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"]


# Provider and model of every role that has its own settings, per profile, read from the model catalog
# (`models.toml`), plus the model of each kind of agent session (`implementer`, `verifier`, `escalation`).
# `balanced` is the field defaults of `Settings` (a test keeps them equal). The panel's critics stay on the
# providers they are chosen for; only the model tier changes.
PROFILES: dict[ModelProfile, dict[str, tuple[Provider, str]]] = {
    name: get_catalog().profile_choices(name) for name in get_args(ModelProfile)
}


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

    # A cost/quality preset for the models of every role below and of the agent sessions. A role whose
    # `*_PROVIDER` or `*_MODEL` is set explicitly ignores it (see `roles.resolve_role_model` and
    # `roles.resolve_agent_model`).
    model_profile: ModelProfile = "balanced"
    # Negotiated or otherwise non-list prices: a TOML file with one [prices."<model id>"] table per model
    # (input_per_mtok, output_per_mtok, optional cached_input_per_mtok). It wins over models.toml (see pricing.py).
    model_prices_file: Path | None = None

    pm_provider: Provider = "openai"
    tech_lead_provider: Provider = "anthropic"
    skeptic_provider: Provider = "google"
    summarizer_provider: Provider = "anthropic"
    architect_provider: Provider = "anthropic"
    moderator_provider: Provider = "anthropic"

    pm_model: str = "gpt-6.1-sol"
    tech_lead_model: str = "claude-sonnet-5-5"
    skeptic_model: str = "gemini-3.8-flash"
    summarizer_model: str = "claude-sonnet-5-5"
    architect_model: str = "claude-opus-5-5"  # also strategy, plan writer, change planner, blueprint critic
    moderator_model: str = "claude-haiku-5-5"  # a cheap call after every panel turn
    # The optional research step (off by default) needs a provider with a native web-search tool: the
    # provider's own tool is used (Anthropic web search, OpenAI web search, Gemini Google Search grounding).
    research_provider: Provider = "anthropic"
    research_model: str = "claude-sonnet-5-5"

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
    acceptance_max_tokens: int = 8000  # the held-out acceptance suite: several test files in one structured answer
    default_rounds: int = 3
    # Extra passes the blueprint critic may trigger: blocker-flagged documents are regenerated with the
    # issues appended. 0 = review and report only.
    max_blueprint_revisions: int = Field(default=1, ge=0)
    # moderated: parallel openings, then a moderator picks speakers and may end the debate early.
    # round_robin: the classic fixed PM -> Tech Lead -> Skeptic rotation for the full turn budget.
    panel_mode: PanelMode = "moderated"
    # A short, cited market/competitor brief gathered with the provider's web search before the panel starts.
    enable_research: bool = False
    research_max_searches: int = Field(default=5, ge=1)

    # Implementation stage (Claude Agent SDK). Requires ANTHROPIC_API_KEY (or a
    # logged-in Claude Code install) and spends real tokens; every run is gated
    # behind explicit user approval in the UI. One model per kind of session: task sessions; the read-mostly
    # verification lanes; the hard cases (merge-conflict resolver, every fix attempt after the first). Setting
    # only IMPLEMENTER_MODEL runs every kind on it, as before the split.
    implementer_model: str = "claude-sonnet-5-5"
    verifier_model: str = "claude-sonnet-5-5"
    escalation_model: str = "claude-opus-5-5"
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
    # What happens when a session deletes, renames, skips, or weakens a test (implementation/integrity.py):
    # `block` fails the verdict until the test is restored or the change is justified in
    # quality/TEST_CHANGES.md, `warn` only reports it, `off` does not check.
    test_integrity: Literal["block", "warn", "off"] = "block"
    # Held-out acceptance tests (implementation/acceptance.py): written from the PRD and the plan's public contracts
    # before each version is built, kept outside the workspace, and run by a fourth verification lane. Unset: on,
    # except in demo mode.
    acceptance_tests: Literal["on", "off"] | None = None
    # Behavioral Contrast (nodes/harness_tuning.py): before the first version is built, run one pilot task under the
    # pack's agent harness and under two proposed edits of it (how the agent works; what a practitioner of the stack
    # knows), then keep the base or adopt what the trajectories show helped. `pilot` turns it on for every run; the
    # implement gate shows its extra cost and can switch it for one run. Its sessions are paid from the whole-run
    # budget, at most half of it.
    harness_tuning: Literal["off", "pilot"] = "off"
    # The experience library (library.py, nodes/reflect.py): after every delivery a reflection records what the run's
    # evidence says about the harness edits and lessons it used, and at most one lesson, under
    # OUTPUT_DIR/library/<stack>/; the next project of that stack gives the index to its planner and harness editors as
    # reference data. One planning call per delivery. Unset: on, except in demo mode (canned lessons would otherwise
    # reach real runs).
    experience_library: Literal["on", "off"] | None = None
    # Publishing a delivery to GitHub (publish.py): opt-in at the iterate gate or with `idea-to-mvp publish`, previewed
    # and confirmed each time. A token that can create repositories (kept out of the agents' environment), else the
    # `gh` CLI's sign-in; the owner is a user or organisation (default: the token's own account); repositories are
    # private unless the request says public.
    github_token: str | None = Field(default=None, validation_alias="GITHUB_TOKEN")
    github_owner: str | None = None
    publish_visibility: Literal["private", "public"] = "private"
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
    def acceptance_enabled(self) -> bool:
        """Whether versions get a held-out acceptance suite (ACCEPTANCE_TESTS, else on outside demo mode)."""
        return self.acceptance_tests == "on" if self.acceptance_tests is not None else not self.demo_mode

    @property
    def library_enabled(self) -> bool:
        """Whether runs read and write the experience library (EXPERIENCE_LIBRARY, else on outside demo mode)."""
        return self.experience_library == "on" if self.experience_library is not None else not self.demo_mode

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

    @property
    def library_dir(self) -> Path:
        return self.output_dir / "library"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache() -> None:
    get_settings.cache_clear()

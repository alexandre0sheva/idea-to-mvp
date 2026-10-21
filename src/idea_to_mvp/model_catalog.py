"""The model catalog: which models exist, what they cost, what they can do, and when that was last checked.

The data lives in `models.toml` next to this file and is validated on load. Everything else that needs model
knowledge (the profiles in `config.py`, sampling in `llm/providers.py`, `doctor`, the UI, `idea-to-mvp models`)
asks this module. It imports nothing from the rest of the package, so `config.py` can build on it.
"""

from __future__ import annotations

import json
import re
import tomllib
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, Literal, NamedTuple, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Provider = Literal["openai", "anthropic", "google"]
Tier = Literal["fast", "balanced", "frontier"]
Status = Literal["active", "legacy", "deprecated"]
FindingKind = Literal["unknown", "deprecated", "outdated", "unavailable"]
# The kinds of agent session (Claude Agent SDK), each with its own model: task sessions, the read-mostly
# verification lanes, and the hard cases (merge resolver, fix attempts after the first).
AgentRole = Literal["implementer", "verifier", "escalation"]
AGENT_ROLES: tuple[AgentRole, ...] = get_args(AgentRole)
_TOKENS_PER_MTOK = 1_000_000

# Current generations reject (Claude Opus 4.7+/5.x, GPT-5.x reasoning) or discourage (Gemini 3.x) custom sampling
# parameters. Used only for a model the catalog does not list; a listed model says so itself.
_UNLISTED_NO_SAMPLING = re.compile(
    r"^(claude-(opus-(5|4-[78])|sonnet-5|haiku-5|fable|mythos)|gemini-3|gpt-[5-9])",
    flags=re.IGNORECASE,
)


class ModelEntry(BaseModel):
    """One model: who serves it, what it costs per million tokens, and what it supports."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    aliases: list[str] = []
    provider: Provider
    tier: Tier
    status: Status = "active"
    retires: date | None = None  # a firm retirement date, only when the provider has announced one
    input_per_mtok: float | None = Field(default=None, ge=0)
    output_per_mtok: float | None = Field(default=None, ge=0)
    cached_input_per_mtok: float | None = Field(default=None, ge=0)
    context_window: int = Field(gt=0)
    max_output: int = Field(gt=0)
    structured_output: bool
    sampling: bool  # accepts temperature and friends
    web_search: bool
    checked: date
    source: str
    notes: str = ""

    @field_validator("source")
    @classmethod
    def _web_address(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("source must be an http(s) address")
        return value

    @model_validator(mode="after")
    def _priced_in_both_directions(self) -> ModelEntry:
        if (self.input_per_mtok is None) != (self.output_per_mtok is None):
            raise ValueError(f"{self.id}: give both input_per_mtok and output_per_mtok, or neither")
        return self

    @property
    def priced(self) -> bool:
        return self.input_per_mtok is not None and self.output_per_mtok is not None

    @property
    def names(self) -> set[str]:
        return {self.id, *self.aliases}

    def retired(self, today: date) -> bool:
        return self.retires is not None and self.retires <= today

    def cost(self, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float | None:
        """Dollars for a call (`cached_tokens` is the part of the input served from the cache); None if unpriced."""
        if self.input_per_mtok is None or self.output_per_mtok is None:
            return None
        cached_rate = (
            self.input_per_mtok
            if self.cached_input_per_mtok is None
            else self.cached_input_per_mtok
        )
        fresh = max(input_tokens - cached_tokens, 0)
        total = (
            fresh * self.input_per_mtok
            + cached_tokens * cached_rate
            + output_tokens * self.output_per_mtok
        )
        return total / _TOKENS_PER_MTOK


class RunShape(BaseModel):
    """What one role does in a typical run: how many calls, and the tokens each one reads and writes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    calls: int = Field(gt=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_fraction: float = Field(default=0.0, ge=0, le=1)


@dataclass(frozen=True)
class RunEstimate:
    """The estimated dollars of a typical run: planning calls, agent sessions, and the roles left out as unpriced."""

    planning: float = 0.0
    agents: float = 0.0
    unpriced: list[str] = field(default_factory=list)

    @property
    def total(self) -> float:
        return self.planning + self.agents


class ModelCatalog(BaseModel):
    """Every known model, the model each profile gives each role, and the shape of a typical run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    models: dict[str, ModelEntry]
    profiles: dict[str, dict[str, str]]
    typical_run: dict[str, RunShape]

    @model_validator(mode="before")
    @classmethod
    def _models_by_id(cls, data: Any) -> Any:
        """The file lists models as an array of tables; index them by id and refuse duplicates."""
        if isinstance(data, dict) and isinstance(data.get("models"), list):
            indexed: dict[str, Any] = {}
            for item in data["models"]:
                model_id = item.get("id") if isinstance(item, dict) else None
                if model_id in indexed:
                    raise ValueError(f"duplicate model id {model_id!r}")
                indexed[str(model_id)] = item
            return {**data, "models": indexed}
        return data

    @model_validator(mode="after")
    def _consistent(self) -> ModelCatalog:
        seen: dict[str, str] = {}
        for model_id, item in self.models.items():
            for name in item.names:
                if name in seen and seen[name] != model_id:
                    raise ValueError(
                        f"alias or id {name!r} of {model_id!r} is already used by {seen[name]!r}"
                    )
                seen[name] = model_id
        role_sets = {frozenset(roles) for roles in self.profiles.values()}
        if len(role_sets) > 1:
            raise ValueError("every profile must cover the same roles")
        for profile, roles in self.profiles.items():
            for role, model_id in roles.items():
                if model_id not in self.models:
                    raise ValueError(
                        f"profile {profile!r} gives role {role!r} the unknown model {model_id!r}"
                    )
        known_roles = set(next(iter(self.profiles.values()), {})) | set(AGENT_ROLES)
        stray = set(self.typical_run) - known_roles
        if stray:
            raise ValueError(f"typical_run names roles no profile has: {sorted(stray)}")
        return self

    def find(self, model_id: str) -> ModelEntry | None:
        """The entry for an id or an alias (surrounding whitespace ignored); None for a model not listed."""
        name = (model_id or "").strip()
        if not name:
            return None
        return next((item for item in self.models.values() if name in item.names), None)

    def profile_choices(self, profile: str) -> dict[str, tuple[Provider, str]]:
        """Role -> (provider, model) for a profile; each model's provider comes from its own entry."""
        return {
            role: (self.models[model_id].provider, model_id)
            for role, model_id in self.profiles[profile].items()
        }

    def estimate_run(self, profile: str, models: Mapping[str, str] | None = None) -> RunEstimate:
        """The cost of a typical run on `profile`'s models; `models` (role -> model id) replaces some of them.

        A role with no model in either place is left out; one whose model is unknown or unpriced is listed in
        `unpriced`. Agent sessions (`AGENT_ROLES`) are summed apart from the planning calls.
        """
        chosen = {**self.profiles[profile], **(models or {})}
        planning = 0.0
        agents = 0.0
        unpriced: list[str] = []
        for role, shape in self.typical_run.items():
            if role not in chosen:
                continue
            item = self.find(chosen[role])
            per_call = None
            if item is not None:
                cached = round(shape.input_tokens * shape.cached_fraction)
                per_call = item.cost(shape.input_tokens, shape.output_tokens, cached)
            if per_call is None:
                unpriced.append(role)
            elif role in AGENT_ROLES:
                agents += per_call * shape.calls
            else:
                planning += per_call * shape.calls
        return RunEstimate(planning, agents, unpriced)


def load_catalog(path: Path | None = None) -> ModelCatalog:
    """Read and validate a catalog file (the packaged `models.toml` by default)."""
    text = (
        path.read_text(encoding="utf-8")
        if path
        else resources.files("idea_to_mvp").joinpath("models.toml").read_text(encoding="utf-8")
    )
    return ModelCatalog.model_validate(tomllib.loads(text))


@lru_cache(maxsize=1)
def get_catalog() -> ModelCatalog:
    return load_catalog()


def accepts_sampling(model: str, catalog: ModelCatalog | None = None) -> bool:
    """Whether a model takes `temperature`: the catalog says for a listed model, the name pattern for the rest."""
    name = (model or "").strip()
    listed = (catalog or get_catalog()).find(name)
    if listed is not None:
        return listed.sampling
    return not _UNLISTED_NO_SAMPLING.match(name)


# ---------------------------------------------------------------- family and version


class ParsedModelId(NamedTuple):
    family: tuple[
        str, ...
    ]  # the name tokens: ("claude", "sonnet"), ("gpt", "mini"), ("gemini", "flash")
    version: tuple[
        int, ...
    ]  # the numbers in order: claude-sonnet-5-5 -> (5, 5), gpt-6.1-sol -> (6, 1)
    preview: (
        bool  # a preview / latest / experimental build, which never counts as the newer release
    )


_DATE_SUFFIX = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2})$")
_VERSION_TOKEN = re.compile(r"\d+(?:\.\d+)*")
_PREVIEW_TOKENS = {"preview", "latest", "exp", "experimental"}


def strip_snapshot_date(model_id: str) -> str:
    """The id without a trailing snapshot date (`claude-sonnet-5-5-20260301` -> `claude-sonnet-5-5`)."""
    return _DATE_SUFFIX.sub("", model_id.strip())


def parse_model_id(model_id: str) -> ParsedModelId:
    """Split an id into a family and a version, ignoring snapshot dates, so ids of one lineage can be compared."""
    name = _DATE_SUFFIX.sub("", model_id.strip().lower().removeprefix("models/"))
    family: list[str] = []
    version: list[int] = []
    preview = False
    for token in name.split("-"):
        if _VERSION_TOKEN.fullmatch(token):
            version.extend(int(part) for part in token.split("."))
        elif token in _PREVIEW_TOKENS:
            preview = True
        elif token:
            family.append(token)
    return ParsedModelId(tuple(family), tuple(version), preview)


# ------------------------------------------------------------------- live listing

Fetch = Callable[[str, dict[str, str]], dict[str, Any]]


def http_get_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    """GET a JSON document (a models list); errors propagate to the caller."""
    request = urllib.request.Request(url, headers=headers)  # fixed https endpoints only
    with urllib.request.urlopen(request, timeout=20) as response:
        payload: dict[str, Any] = json.load(response)
    return payload


def list_provider_models(provider: Provider, api_key: str, fetch: Fetch | None = None) -> list[str]:
    """The model ids a provider offers this key, from its models API. The key travels in a header, never in the URL."""
    get = fetch or http_get_json
    ids: list[str] = []
    if provider == "anthropic":
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        url = "https://api.anthropic.com/v1/models?limit=1000"
        while True:
            page = get(url, headers)
            ids += [str(item["id"]) for item in page.get("data", [])]
            if not page.get("has_more") or not page.get("last_id"):
                return ids
            url = f"https://api.anthropic.com/v1/models?limit=1000&after_id={page['last_id']}"
    if provider == "openai":
        page = get("https://api.openai.com/v1/models", {"Authorization": f"Bearer {api_key}"})
        return [str(item["id"]) for item in page.get("data", [])]
    headers = {"x-goog-api-key": api_key}
    url = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000"
    while True:
        page = get(url, headers)
        ids += [str(item["name"]).removeprefix("models/") for item in page.get("models", [])]
        if not page.get("nextPageToken"):
            return ids
        url = f"https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000&pageToken={page['nextPageToken']}"


@dataclass(frozen=True)
class Finding:
    """A problem `models --check` found with a configured model."""

    kind: FindingKind
    model: str
    roles: list[str]
    detail: str


def check_models(
    configured: Sequence[tuple[str, str, str]],
    listings: Mapping[str, Sequence[str] | Exception],
    catalog: ModelCatalog,
    *,
    today: date | None = None,
) -> list[Finding]:
    """Compare configured `(role, provider, model)` triples with what each provider lists today.

    Flags a model the provider does not list (`unknown`), one the catalog marks deprecated or past its retirement
    date (`deprecated`), one with a newer release of the same family on the provider's list (`outdated`), and a
    provider whose list could not be read (`unavailable`). A provider missing from `listings` is not checked.
    """
    today = today or date.today()
    grouped: dict[tuple[str, str], list[str]] = {}
    for role, provider, model in configured:
        grouped.setdefault((provider, model), []).append(role)

    findings: list[Finding] = []
    unavailable: set[str] = set()
    for (provider, model), roles in grouped.items():
        listing = listings.get(provider)
        if listing is None:
            continue
        if isinstance(listing, Exception):
            if provider not in unavailable:
                unavailable.add(provider)
                affected = [r for (p, _), rs in grouped.items() if p == provider for r in rs]
                findings.append(
                    Finding("unavailable", provider, affected, f"could not list models: {listing}")
                )
            continue
        listed = set(listing)
        item = catalog.find(model)
        names = item.names if item else {model}
        if not names & listed:
            findings.append(
                Finding("unknown", model, roles, f"{provider} does not list this model")
            )
        if item and (item.status == "deprecated" or item.retired(today)):
            when = f" (retires {item.retires})" if item.retires else ""
            findings.append(
                Finding("deprecated", model, roles, f"the catalog marks it deprecated{when}")
            )
        mine = parse_model_id(model)
        newer = [
            candidate
            for candidate in listed
            if not (parsed := parse_model_id(candidate)).preview
            and parsed.family == mine.family
            and parsed.version > mine.version
        ]
        if newer:
            best = max(newer, key=lambda candidate: parse_model_id(candidate).version)
            findings.append(
                Finding(
                    "outdated", model, roles, f"a newer model of the same family exists: {best}"
                )
            )
    return findings

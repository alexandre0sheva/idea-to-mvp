"""Dollars for model calls and a dollar range for an implementation run.

Prices come from the model catalog (`models.toml`); `MODEL_PRICES_FILE` replaces them per model, for negotiated
or in-house prices. A model with no price anywhere stays unpriced (`None`): the UI says so instead of showing a
figure that leaves it out silently. Agent sessions are not priced here, the Agent SDK reports their cost.

The implement gate's estimate is a range: the tasks of the plan times the cost of a task session (the median of
earlier projects' finished tasks on the same model when there are enough, else the catalog's typical session),
plus one verification round, with fix attempts only in the high end, and never more than the whole-run cap.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import TYPE_CHECKING, Any, Literal

from idea_to_mvp.implementation.ledger import load_ledger
from idea_to_mvp.model_catalog import ModelCatalog, get_catalog, strip_snapshot_date

if TYPE_CHECKING:  # pragma: no cover - typing only
    from idea_to_mvp.config import Settings

_TOKENS_PER_MTOK = 1_000_000
_PRICE_KEYS = ("input_per_mtok", "output_per_mtok", "cached_input_per_mtok")
# How far a real run may stray from the typical session, as factors on the per-task cost: wide when the figure
# is the catalog's guess, narrower when it is the median of tasks this machine has actually built.
CATALOG_BAND = (0.5, 2.0)
HISTORY_BAND = (0.75, 1.5)
MIN_HISTORY = 3  # earlier tasks needed before they replace the catalog's guess

Basis = Literal["history", "catalog", "unpriced"]


class PricingError(ValueError):
    """The prices file cannot be used."""


@dataclass(frozen=True)
class Price:
    """Dollars per million tokens of one model."""

    input_per_mtok: float
    output_per_mtok: float
    cached_input_per_mtok: float | None = None

    def cost(self, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float:
        """Dollars for a call; `cached_tokens` is the part of the input served from the provider's cache."""
        cached_rate = self.input_per_mtok if self.cached_input_per_mtok is None else self.cached_input_per_mtok
        fresh = max(input_tokens - cached_tokens, 0)
        return (fresh * self.input_per_mtok + cached_tokens * cached_rate + output_tokens * self.output_per_mtok) / _TOKENS_PER_MTOK


# ------------------------------------------------------------------- overrides


def _number(path: Path, model: str, key: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PricingError(f"{path}: {key} of {model!r} must be a number")
    if value < 0:
        raise PricingError(f"{path}: {key} of {model!r} must not be negative")
    return float(value)


def load_price_overrides(path: Path) -> dict[str, Price]:
    """Read a prices file: one `[prices."<model id>"]` table per model. Raises `PricingError` with the reason."""
    path = Path(path).expanduser()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PricingError(f"cannot read the prices file {path}: {exc.strerror or exc}") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PricingError(f"{path} is not valid TOML: {exc}") from exc
    table = data.get("prices")
    if not isinstance(table, dict) or not table:
        raise PricingError(f'{path} needs a [prices."<model id>"] table for each model it prices')
    found: dict[str, Price] = {}
    for model, fields in table.items():
        if not isinstance(fields, dict):
            raise PricingError(f'{path}: [prices."{model}"] must be a table')
        unknown = sorted(set(fields) - set(_PRICE_KEYS))
        if unknown:
            raise PricingError(f"{path}: unknown key {', '.join(unknown)} for {model!r} (use {', '.join(_PRICE_KEYS)})")
        if "input_per_mtok" not in fields or "output_per_mtok" not in fields:
            raise PricingError(f"{path}: give both input_per_mtok and output_per_mtok for {model!r}")
        cached = fields.get("cached_input_per_mtok")
        found[str(model)] = Price(
            _number(path, model, "input_per_mtok", fields["input_per_mtok"]),
            _number(path, model, "output_per_mtok", fields["output_per_mtok"]),
            None if cached is None else _number(path, model, "cached_input_per_mtok", cached),
        )
    return found


def settings_overrides(settings: Settings) -> dict[str, Price]:
    """The prices file the settings name ({} without one). Raises `PricingError` when it is unusable."""
    return load_price_overrides(settings.model_prices_file) if settings.model_prices_file else {}


# ------------------------------------------------------------------ one call


def price_for(model: str, overrides: dict[str, Price] | None = None, catalog: ModelCatalog | None = None) -> Price | None:
    """The price of a model: the prices file first, then the catalog; a dated snapshot counts as its model."""
    name = (model or "").strip()
    if not name:
        return None
    catalog = catalog or get_catalog()
    base = strip_snapshot_date(name)
    entry = catalog.find(name) or catalog.find(base)
    if overrides:
        for candidate in (name, base, *((entry.id, *entry.aliases) if entry else ())):
            if candidate in overrides:
                return overrides[candidate]
    if entry is not None and entry.input_per_mtok is not None and entry.output_per_mtok is not None:
        return Price(entry.input_per_mtok, entry.output_per_mtok, entry.cached_input_per_mtok)
    return None


def call_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
    *,
    overrides: dict[str, Price] | None = None,
) -> float | None:
    """Dollars for one model call; None when the model has no price."""
    price = price_for(model, overrides)
    return None if price is None else price.cost(input_tokens, output_tokens, cached_tokens)


# --------------------------------------------------------------------- history


def task_cost_history(projects_dir: Path, model: str) -> list[float]:
    """What each finished task of earlier projects cost on `model`, summed over its attempts (from their ledgers).

    A task counts once it has a successful session; a resolver or lane is not the task's own session.
    """
    try:
        folders = sorted(p for p in Path(projects_dir).iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        return []
    costs: list[float] = []
    for folder in folders:
        spent: dict[str, float] = {}
        finished: set[str] = set()
        for row in load_ledger(folder):
            if row["kind"] != "task" or row["model"] != model or not row["task_id"]:
                continue
            spent[row["task_id"]] = spent.get(row["task_id"], 0.0) + row["cost_usd"]
            if row["outcome"] == "success":
                finished.add(row["task_id"])
        costs += [spent[task_id] for task_id in sorted(finished) if spent[task_id] > 0]
    return costs


# -------------------------------------------------------------------- estimate


@dataclass(frozen=True)
class CostRange:
    """The implement gate's estimate of what a run will cost."""

    low: float | None
    high: float | None
    per_task: float | None
    basis: Basis
    samples: int  # earlier tasks the figure rests on
    tasks: int
    unpriced: list[str] = field(default_factory=list)  # kinds of agent session with no price

    def to_dict(self) -> dict[str, Any]:
        return {
            "low": self.low, "high": self.high, "per_task": self.per_task, "basis": self.basis,
            "samples": self.samples, "tasks": self.tasks, "unpriced": list(self.unpriced),
        }  # fmt: skip


def _typical_session(settings: Settings, role: Any, overrides: dict[str, Price], catalog: ModelCatalog) -> float | None:
    """The catalog's typical session of an agent role, priced on the model it runs on (None: unpriced)."""
    from idea_to_mvp.roles import resolve_agent_model

    shape = catalog.typical_run.get(role) or catalog.typical_run["implementer"]
    price = price_for(resolve_agent_model(settings, role), overrides, catalog)
    if price is None:
        return None
    return price.cost(shape.input_tokens, shape.output_tokens, round(shape.input_tokens * shape.cached_fraction))


def estimate_tuning_cost(settings: Settings, estimate: CostRange) -> dict[str, Any]:
    """What harness tuning adds to a run (`nodes/harness_tuning.py`): three pilot task sessions at the run estimate's
    per-task cost and band, a quick tests lane after each, and three calls on the harness editor's model (two
    proposals, one composition). The sessions never cost more than their share of the whole-run budget.
    `low` / `high` are None when a model has no price; `sessions_low` / `sessions_high` are the agent sessions alone
    (what the dashboard sets against the agent spend; the calls are planning spend)."""
    from idea_to_mvp.harness_tuning import TUNING_SHARE
    from idea_to_mvp.roles import resolve_role_model

    catalog = get_catalog()
    try:
        overrides = settings_overrides(settings)
    except PricingError:
        overrides = {}
    rollouts, calls = 3, 3
    lane = _typical_session(settings, "verifier", overrides, catalog)
    shape = catalog.typical_run["architect"]
    price = price_for(resolve_role_model(settings, "harness_editor")[1], overrides, catalog)
    call = price.cost(shape.input_tokens, shape.output_tokens, round(shape.input_tokens * shape.cached_fraction)) if price else None
    cap = settings.implementer_max_total_usd * TUNING_SHARE
    result: dict[str, Any] = {
        "low": None, "high": None, "sessions_low": None, "sessions_high": None, "rollouts": rollouts, "calls": calls, "cap": cap,
    }  # fmt: skip
    if estimate.per_task is None or lane is None:
        return result
    band = HISTORY_BAND if estimate.basis == "history" else CATALOG_BAND
    result["sessions_low"] = min(rollouts * estimate.per_task * band[0] + rollouts * lane, cap)
    result["sessions_high"] = min(rollouts * estimate.per_task * band[1] + rollouts * lane, cap)
    if call is not None:
        result["low"] = result["sessions_low"] + calls * call
        result["high"] = result["sessions_high"] + calls * call
    return result


def with_tuning(estimate: dict[str, Any], tuning: dict[str, Any], cap: float) -> dict[str, Any]:
    """A run estimate (`CostRange.to_dict()`) with harness tuning's agent sessions added, so the dashboard sets the
    agent spend of a tuned run against both. Still capped by the whole-run budget, which the tuning shares."""
    low, high = estimate.get("low"), estimate.get("high")
    extra_low, extra_high = tuning.get("sessions_low"), tuning.get("sessions_high")
    if low is None or high is None or extra_low is None or extra_high is None:
        return {**estimate, "harness_tuning": True}
    return {**estimate, "low": min(low + extra_low, cap), "high": min(high + extra_high, cap), "harness_tuning": True}


def estimate_run_cost(settings: Settings, task_count: int, history: list[float] | None = None) -> CostRange:
    """The dollar range of building `task_count` tasks, verifying once, and (high end only) fixing up to
    `MAX_FIX_ATTEMPTS` times; `history` is `task_cost_history` for the task model."""
    catalog = get_catalog()
    try:
        overrides = settings_overrides(settings)
    except PricingError:
        overrides = {}  # `doctor` reports the file; the estimate falls back to the catalog
    tasks = max(task_count, 1)
    fixes = settings.max_fix_attempts

    def session(role: Any) -> float | None:
        return _typical_session(settings, role, overrides, catalog)

    task_session, lane, hard = session("implementer"), session("verifier"), session("escalation")
    needed = {"implementer": task_session, "verifier": lane, **({"escalation": hard} if fixes else {})}
    unpriced = [role for role, cost in needed.items() if cost is None]
    if unpriced or task_session is None or lane is None:
        return CostRange(None, None, None, "unpriced", 0, tasks, unpriced)

    samples = len(history or [])
    if history and samples >= MIN_HISTORY:
        per_task, band, basis = float(median(history)), HISTORY_BAND, "history"
    else:
        per_task, band, basis, samples = task_session, CATALOG_BAND, "catalog", 0
    lanes = catalog.typical_run["verifier"].calls + (1 if settings.acceptance_enabled else 0)  # + the acceptance lane
    verification = lane * lanes  # one verification round
    low = tasks * per_task * band[0] + verification
    high = tasks * per_task * band[1] + verification
    for attempt in range(1, fixes + 1):  # the first fix runs on the task model, later ones escalate
        high += (task_session if attempt == 1 else hard or task_session) + verification
    cap = settings.implementer_max_total_usd
    return CostRange(min(low, cap), min(high, cap), per_task, basis, samples, tasks)  # type: ignore[arg-type]

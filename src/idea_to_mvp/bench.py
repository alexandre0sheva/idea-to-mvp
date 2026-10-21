"""`idea-to-mvp bench`: the golden ideas through the headless pipeline, scored and comparable.

One bench *run* is a folder `<OUTPUT_DIR>/bench/<name>/` with its own output root (sessions, workspaces, library),
a `config.json` snapshot of the settings it ran under, and a `results.json` that grows one idea at a time. The
discipline is Harness²'s: a run folder refuses to continue under different settings, two configurations never share
an output root (so one cannot learn from the other's library), an idea that failed to run is *unscored* (an
infrastructure failure is not a zero; a delivery whose verification failed is a real zero), and `--compare` scores
only the ideas every run scored. Each idea is a headless run (`headless.py`) with autopilot and `--implement`, so
the bench needs `--live` (real providers, with a cap on the whole bench) or `DEMO_MODE=true` (offline smoke test).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean
from typing import Any

from pydantic import ValidationError

from idea_to_mvp.config import Settings, clear_settings_cache, get_settings
from idea_to_mvp.headless import HeadlessOptions, RunSummary, resume_thread, run_idea
from idea_to_mvp.model_catalog import get_catalog
from idea_to_mvp.model_report import configured_choices
from idea_to_mvp.resilience import error_hint
from idea_to_mvp.schemas import ProjectPreferences

DEFAULT_IDEAS = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "golden_ideas.json"
SNAPSHOT_VERSION = 1
MIN_BUDGET_USD = 0.01  # below a cent left is no budget: the idea is not started

# Settings the bench owns: the run folder decides where things are kept, `--live` / DEMO_MODE decides the mode,
# and `--max-usd-per-idea` sets the per-idea budget.
OWNED_SETTINGS = frozenset(
    {"OUTPUT_DIR", "CHECKPOINT_DB", "CHECKPOINTER", "DEMO_MODE", "IMPLEMENTER_MAX_TOTAL_USD"}
)
# What the snapshot records beyond the resolved models: every setting that can change a result or its cost.
SNAPSHOT_SETTINGS = (
    "model_profile",
    "harness_tuning",
    "test_integrity",
    "max_fix_attempts",
    "implementer_max_total_usd",
    "implementer_max_task_usd",
    "implementer_max_parallel",
    "implementer_max_turns",
    "implementer_max_task_turns",
    "verifier_max_turns",
    "implementer_permission_mode",
    "implementer_sandbox",
    "enable_research",
    "default_rounds",
    "panel_mode",
    "max_iterations",
)

EXIT_ALL_VERIFIED = 0
EXIT_ERROR = 1
EXIT_NOT_VERIFIED = 2
EXIT_INCOMPLETE = 3

Progress = Callable[[str], None]


class BenchError(Exception):
    """A bench call that cannot proceed; the message tells the user what to change."""


# ------------------------------------------------------------------ ideas


@dataclass(frozen=True)
class BenchIdea:
    id: str
    idea: str
    preferences: Mapping[str, Any] = field(default_factory=dict)

    @property
    def sha(self) -> str:
        """A fingerprint of what the pipeline is given, so a changed idea is not compared with the old one."""
        text = json.dumps([self.idea, dict(self.preferences)], sort_keys=True)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def load_ideas(path: Path, ids: Sequence[str] | None = None) -> list[BenchIdea]:
    """The ideas in `path` (all of them, or `ids` in the order given); an unknown id is an error."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BenchError(f"cannot read the ideas file {path}: {exc}") from exc
    try:
        ideas = [
            BenchIdea(
                str(item["id"]),
                str(item["idea"]),
                dict(ProjectPreferences(**item.get("preferences", {})).model_dump()),
            )
            for item in raw
        ]
    except (KeyError, TypeError, ValidationError) as exc:
        raise BenchError(
            f"{path} is not a list of {{id, idea, preferences}} objects: {exc}"
        ) from exc
    if ids is None:
        return ideas
    known = {idea.id: idea for idea in ideas}
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise BenchError(
            f"unknown idea id(s) {', '.join(unknown)}; the file has: {', '.join(known)}"
        )
    return [known[i] for i in dict.fromkeys(ids)]


# ------------------------------------------------------------------ settings for one run


def parse_overrides(pairs: Sequence[str]) -> dict[str, str]:
    """`KEY=VALUE` pairs as environment-style overrides of the settings (`MODEL_PROFILE=fast`)."""
    overrides: dict[str, str] = {}
    for pair in pairs:
        key, separator, value = pair.partition("=")
        key = key.strip().upper()
        if not separator or not key:
            raise BenchError(f"--set takes KEY=VALUE, not {pair!r}")
        if key in OWNED_SETTINGS:
            raise BenchError(
                f"{key} is set by the bench itself (the run folder, --live, --max-usd-per-idea)"
            )
        if key.lower() not in Settings.model_fields:
            raise BenchError(f"--set {key}: not a setting (see .env.example)")
        overrides[key] = value.strip()
    return overrides


@contextmanager
def applied_env(overrides: Mapping[str, str]) -> Iterator[Settings]:
    """The settings with `overrides` in the environment; the environment is restored on exit."""
    saved = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    clear_settings_cache()
    try:
        try:
            yield get_settings()
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']).upper()}: {e['msg']}" for e in exc.errors()
            )
            raise BenchError(f"invalid settings: {problems}") from exc
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        clear_settings_cache()


def settings_snapshot(settings: Settings) -> dict[str, Any]:
    """What a run is configured with, flat: the listed settings, the on/off switches that default by mode, and the
    model every role and kind of agent session resolves to."""
    flat: dict[str, Any] = {name: getattr(settings, name) for name in SNAPSHOT_SETTINGS}
    flat["library"] = "on" if settings.library_enabled else "off"
    flat["acceptance"] = "on" if settings.acceptance_enabled else "off"
    flat.update(
        {f"models.{role}": model for role, model in sorted(configured_choices(settings).items())}
    )
    return flat


def take_snapshot(
    settings: Settings, overrides: Mapping[str, str], ideas: Sequence[BenchIdea]
) -> dict[str, Any]:
    return {
        "version": SNAPSHOT_VERSION,
        "mode": "demo" if settings.demo_mode else "live",
        "settings": settings_snapshot(settings),
        "overrides": dict(overrides),
        "ideas": [{"id": idea.id, "sha": idea.sha} for idea in ideas],
    }


def snapshot_differences(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[str]:
    """One line per way `new` differs from the `old` snapshot of a run folder (empty: the same experiment)."""
    lines: list[str] = []
    for key in ("mode", "overrides"):
        if old.get(key) != new.get(key):
            lines.append(f"{key}: {old.get(key)!r} -> {new.get(key)!r}")
    old_settings, new_settings = old.get("settings") or {}, new.get("settings") or {}
    for key in sorted({*old_settings, *new_settings}):
        if old_settings.get(key) != new_settings.get(key):
            lines.append(f"{key}: {old_settings.get(key)!r} -> {new_settings.get(key)!r}")
    old_ideas = {item["id"]: item["sha"] for item in old.get("ideas") or []}
    new_ideas = {item["id"]: item["sha"] for item in new.get("ideas") or []}
    if list(old_ideas) != list(new_ideas):
        lines.append(
            f"ideas: the run has [{', '.join(old_ideas)}], this call has [{', '.join(new_ideas)}]"
        )
    else:
        lines += [
            f"idea {key}: its text or preferences changed"
            for key in old_ideas
            if old_ideas[key] != new_ideas[key]
        ]
    return lines


def idea_budget(*, per_idea: float, total: float | None, spent: float) -> float | None:
    """The agent budget for the next idea: the per-idea cap, or what is left of the global cap when that is less;
    None when nothing worth starting an idea with is left."""
    if total is None:
        return per_idea
    left = total - spent
    return None if left < MIN_BUDGET_USD else min(per_idea, left)


# ------------------------------------------------------------------ results


@dataclass
class IdeaResult:
    """One idea's outcome. `scored`: the pipeline delivered and verification decided (`verified` is the verdict);
    `unscored`: it could not run to a delivery (an error, a gate, the budget), which counts for nothing, not zero."""

    id: str
    status: str  # scored | unscored
    verified: bool | None = None
    reason: str = ""
    lanes_passed: int | None = None
    lanes_total: int | None = None
    acceptance_passed: int | None = None
    acceptance_total: int | None = None
    integrity_flags: int = 0
    integrity_open: int = 0
    fix_attempts: int = 0
    planning_cost_usd: float | None = None
    agent_cost_usd: float = 0.0
    unpriced_calls: int = 0
    wall_seconds: float = 0.0
    budget_usd: float | None = None
    exit_code: int | None = None
    thread_id: str = ""
    workspace: str = ""

    @property
    def total_cost_usd(self) -> float:
        return (self.planning_cost_usd or 0.0) + self.agent_cost_usd

    @property
    def acceptance_rate(self) -> float | None:
        if not self.acceptance_total:
            return None
        return (self.acceptance_passed or 0) / self.acceptance_total

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "total_cost_usd": self.total_cost_usd,
            "acceptance_rate": self.acceptance_rate,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> IdeaResult:
        known = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})


def _acceptance_counts(lanes: Sequence[Mapping[str, Any]]) -> tuple[int | None, int | None]:
    """(passed, ran) of the held-out suite from the acceptance lane's parsed report; skipped tests do not count."""
    for lane in lanes:
        evidence = lane.get("evidence")
        if lane.get("lane") == "acceptance" and isinstance(evidence, Mapping):
            passed = int(evidence.get("passed") or 0)
            return passed, passed + int(evidence.get("failed") or 0) + int(
                evidence.get("errors") or 0
            )
    return None, None


def score_run(
    idea_id: str, summary: RunSummary, values: Mapping[str, Any], wall_seconds: float
) -> IdeaResult:
    """The result of one headless run: scored when it delivered, else unscored with the reason."""
    common: dict[str, Any] = {
        "id": idea_id,
        "planning_cost_usd": summary.planning_cost_usd,
        "agent_cost_usd": summary.agent_cost_usd,
        "unpriced_calls": summary.unpriced_calls,
        "wall_seconds": wall_seconds,
        "exit_code": summary.exit_code,
        "thread_id": summary.thread_id,
        "workspace": summary.workspace,
    }
    if summary.outcome not in ("verified", "failed"):
        return IdeaResult(status="unscored", reason=summary.message or summary.outcome, **common)
    verification = values.get("verification") or {}
    lanes = [lane for lane in verification.get("lanes") or [] if isinstance(lane, Mapping)]
    findings = list((values.get("integrity_findings") or {}).values())
    passed, ran = _acceptance_counts(lanes)
    return IdeaResult(
        status="scored",
        verified=summary.outcome == "verified",
        lanes_passed=sum(1 for lane in lanes if lane.get("passed")),
        lanes_total=len(lanes),
        acceptance_passed=passed,
        acceptance_total=ran,
        integrity_flags=len(findings),
        integrity_open=sum(1 for finding in findings if finding.get("status") == "open"),
        fix_attempts=int(verification.get("attempts") or 0),
        **common,
    )


@dataclass
class Totals:
    """A set of results folded into the numbers a comparison turns on. Unscored ideas count in `unscored` and in
    `unscored_spend_usd` only: they are in no rate."""

    ideas: int = 0
    scored: int = 0
    unscored: int = 0
    verified: int = 0
    verified_rate: float | None = None
    lane_pass_rate: float | None = None
    acceptance_rate: float | None = None
    fix_attempts: int = 0
    integrity_flags: int = 0
    spend_usd: float = 0.0
    unscored_spend_usd: float = 0.0
    unpriced_calls: int = 0
    cost_per_idea: float | None = None
    cost_per_verified: float | None = None
    verified_per_usd: float | None = None
    mean_wall_seconds: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def totals(results: Sequence[IdeaResult]) -> Totals:
    scored = [r for r in results if r.status == "scored"]
    verified = sum(1 for r in scored if r.verified)
    spend = sum(r.total_cost_usd for r in scored)
    lanes_total = sum(r.lanes_total or 0 for r in scored)
    accepted = [r for r in scored if r.acceptance_total]
    acceptance_total = sum(r.acceptance_total or 0 for r in accepted)
    return Totals(
        ideas=len(results),
        scored=len(scored),
        unscored=len(results) - len(scored),
        verified=verified,
        verified_rate=verified / len(scored) if scored else None,
        lane_pass_rate=sum(r.lanes_passed or 0 for r in scored) / lanes_total
        if lanes_total
        else None,
        acceptance_rate=sum(r.acceptance_passed or 0 for r in accepted) / acceptance_total
        if acceptance_total
        else None,
        fix_attempts=sum(r.fix_attempts for r in scored),
        integrity_flags=sum(r.integrity_flags for r in scored),
        spend_usd=spend,
        unscored_spend_usd=sum(r.total_cost_usd for r in results if r.status != "scored"),
        unpriced_calls=sum(r.unpriced_calls for r in results),
        cost_per_idea=spend / len(scored) if scored else None,
        cost_per_verified=spend / verified if verified else None,
        verified_per_usd=verified / spend if spend > 0 else None,
        mean_wall_seconds=fmean(r.wall_seconds for r in scored) if scored else None,
    )


# ------------------------------------------------------------------ the run folder


@dataclass
class RunRecord:
    name: str
    snapshot: dict[str, Any]
    results: list[IdeaResult] = field(default_factory=list)

    @property
    def mode(self) -> str:
        return str(self.snapshot.get("mode") or "?")


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_suffix(".tmp")
    scratch.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    scratch.replace(path)  # a bench killed mid-write leaves the old file, not half of a new one


def read_run(run_dir: Path) -> RunRecord | None:
    """A run folder's snapshot and results, or None when `run_dir` holds no bench run."""
    config = run_dir / "config.json"
    if not config.is_file():
        return None
    try:
        snapshot = json.loads(config.read_text(encoding="utf-8"))
        stored = (
            json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
            if (run_dir / "results.json").is_file()
            else {}
        )
    except (OSError, ValueError) as exc:
        raise BenchError(f"{run_dir} is not a readable bench run: {exc}") from exc
    return RunRecord(
        run_dir.name, snapshot, [IdeaResult.from_dict(item) for item in stored.get("results") or []]
    )


def _save_results(
    run_dir: Path, results: Mapping[str, IdeaResult], order: Sequence[str]
) -> list[IdeaResult]:
    ordered = [results[idea_id] for idea_id in order if idea_id in results]
    _write_json(
        run_dir / "results.json",
        {
            "updated": datetime.now(UTC).isoformat(timespec="seconds"),
            "results": [r.to_dict() for r in ordered],
        },
    )
    return ordered


# ------------------------------------------------------------------ running


@dataclass(frozen=True)
class BenchOptions:
    name: str
    root: Path
    ideas: Sequence[BenchIdea]
    overrides: Mapping[str, str] = field(default_factory=dict)
    per_idea_usd: float | None = None  # None: the IMPLEMENTER_MAX_TOTAL_USD setting
    total_usd: float | None = None
    live: bool = False

    @property
    def run_dir(self) -> Path:
        return self.root / self.name


def check_mode(options: BenchOptions, settings: Settings) -> None:
    """The bench spends money unless it is a demo run: it must be asked for explicitly, with a cap."""
    if options.live and settings.demo_mode:
        raise BenchError(
            "DEMO_MODE=true is set, so nothing live can run: drop --live (an offline smoke run), or unset DEMO_MODE"
        )
    if not options.live and not settings.demo_mode:
        raise BenchError(
            "the bench spends money on real models and agent sessions: pass --live (and --max-total-usd N), "
            "or run it offline with DEMO_MODE=true"
        )
    if options.live and options.total_usd is None:
        raise BenchError("--live needs --max-total-usd N: a cap on everything the bench spends")


async def _execute(
    graph: Any,
    registry: Any,
    idea: BenchIdea,
    thread_id: str,
    settings: Settings,
    progress: Progress,
) -> tuple[RunSummary, dict[str, Any]]:
    """One idea through the headless pipeline (continuing its thread if an earlier call left one) and the state
    it ended in."""
    from idea_to_mvp.graph import read_thread

    options = HeadlessOptions(autopilot=True, implement=True)
    if (await read_thread(graph, thread_id)).values.get("user_idea"):
        summary = await resume_thread(
            graph, registry, thread_id, options, settings=settings, progress=progress
        )
    else:
        summary = await run_idea(
            graph,
            registry,
            idea.idea,
            options,
            settings=settings,
            progress=progress,
            thread_id=thread_id,
            preferences=ProjectPreferences(**idea.preferences),
        )
    return summary, dict((await read_thread(graph, thread_id)).values)


def _unscored(idea_id: str, reason: str, **fields_: Any) -> IdeaResult:
    return IdeaResult(id=idea_id, status="unscored", reason=reason, **fields_)


async def run_bench(options: BenchOptions, *, progress: Progress) -> RunRecord:
    """Run the ideas the run folder has not scored yet, saving after each, and return the folder's record."""
    from idea_to_mvp.graph import open_graph
    from idea_to_mvp.observability import apply_tracing_env
    from idea_to_mvp.sessions import SessionRegistry

    base = get_settings()
    check_mode(options, base)
    per_idea = options.per_idea_usd or base.implementer_max_total_usd
    output = options.run_dir / "output"
    env = {
        **options.overrides,
        "OUTPUT_DIR": str(output),
        "CHECKPOINT_DB": str(output / "sessions.db"),
        "IMPLEMENTER_MAX_TOTAL_USD": str(per_idea),
    }
    order = [idea.id for idea in options.ideas]
    with applied_env(env) as settings:
        snapshot = take_snapshot(settings, options.overrides, options.ideas)
        earlier = read_run(options.run_dir)
        if earlier is not None:
            differences = snapshot_differences(earlier.snapshot, snapshot)
            if differences:
                raise BenchError(
                    f"{options.run_dir} was run with different settings; use another --name, or restore them:\n  "
                    + "\n  ".join(differences)
                )
        _write_json(
            options.run_dir / "config.json",
            {**snapshot, "created": datetime.now(UTC).isoformat(timespec="seconds")},
        )
        results = {r.id: r for r in (earlier.results if earlier else [])}
        apply_tracing_env()
        async with open_graph(settings) as graph:
            registry = SessionRegistry(settings.checkpoint_path)
            for idea in options.ideas:
                prior = results.get(idea.id)
                if prior is not None and prior.status == "scored":
                    verdict = "verified" if prior.verified else "not verified"
                    progress(f"[{idea.id}] already scored ({verdict}); kept")
                    continue
                spent = sum(r.total_cost_usd for key, r in results.items() if key != idea.id)
                budget = idea_budget(per_idea=per_idea, total=options.total_usd, spent=spent)
                if budget is None:
                    reason = (
                        f"bench budget: the --max-total-usd cap (${options.total_usd:.2f}) is spent"
                    )
                    progress(f"[{idea.id}] not started: {reason}")
                    results[idea.id] = _unscored(idea.id, reason)
                    _save_results(options.run_dir, results, order)
                    continue
                os.environ["IMPLEMENTER_MAX_TOTAL_USD"] = str(budget)
                clear_settings_cache()
                results[idea.id] = await _run_one(
                    graph, registry, idea, get_settings(), budget, prior, progress
                )
                _save_results(options.run_dir, results, order)
        ordered = _save_results(options.run_dir, results, order)
    return RunRecord(options.name, snapshot, ordered)


async def _run_one(
    graph: Any,
    registry: Any,
    idea: BenchIdea,
    settings: Settings,
    budget: float,
    prior: IdeaResult | None,
    progress: Progress,
) -> IdeaResult:
    progress(f"[{idea.id}] start (agent budget ${budget:.2f})")
    started = time.monotonic()

    def tagged(line: str) -> None:
        progress(f"[{idea.id}] {line}")

    thread_id = f"bench-{idea.id}"
    try:
        summary, values = await _execute(graph, registry, idea, thread_id, settings, tagged)
    except Exception as exc:
        result = _unscored(
            idea.id,
            error_hint("Pipeline", exc),
            thread_id=thread_id,
            wall_seconds=time.monotonic() - started,
        )
    else:
        result = score_run(idea.id, summary, values, time.monotonic() - started)
    result.wall_seconds += (
        prior.wall_seconds if prior else 0.0
    )  # a retried idea's time is all the time it took
    result.budget_usd = budget
    state = (
        "unscored: " + result.reason
        if result.status == "unscored"
        else ("verified" if result.verified else "NOT verified")
    )
    progress(f"[{idea.id}] {state} (${result.total_cost_usd:.2f}, {result.wall_seconds:.0f}s)")
    return result


# ------------------------------------------------------------------ comparing


@dataclass
class Comparison:
    runs: list[RunRecord]
    common_ideas: list[str]
    differences: list[dict[str, Any]]
    notes: list[str]

    def results_on_common(self, run: RunRecord) -> list[IdeaResult]:
        return [r for r in run.results if r.id in self.common_ideas]

    def to_dict(self) -> dict[str, Any]:
        return {
            "runs": [
                {
                    "name": run.name,
                    "mode": run.mode,
                    "totals": totals(self.results_on_common(run)).to_dict(),
                }
                for run in self.runs
            ],
            "common_ideas": self.common_ideas,
            "differences": self.differences,
            "notes": self.notes,
            "ideas": {
                idea_id: {
                    run.name: {
                        "verified": r.verified,
                        "cost_usd": r.total_cost_usd,
                        "fix_attempts": r.fix_attempts,
                    }
                    for run in self.runs
                    for r in run.results
                    if r.id == idea_id
                }
                for idea_id in self.common_ideas
            },
        }


def compare_runs(runs: Sequence[RunRecord]) -> Comparison:
    """Align runs on the ideas every one of them scored (and whose text is the same in all): a rate over different
    ideas would compare the ideas, not the configurations."""
    shas = [{item["id"]: item["sha"] for item in run.snapshot.get("ideas") or []} for run in runs]
    scored = [{r.id for r in run.results if r.status == "scored"} for run in runs]
    notes: list[str] = []
    common: list[str] = []
    for idea_id in dict.fromkeys(i for run in runs for i in (r.id for r in run.results)):
        missing = [run.name for run, ids in zip(runs, scored, strict=True) if idea_id not in ids]
        if missing:
            notes.append(f"{idea_id}: left out, not scored in {', '.join(missing)}")
        elif len({sha.get(idea_id) for sha in shas}) > 1:
            notes.append(f"{idea_id}: left out, its text differs between the runs")
        else:
            common.append(idea_id)
    flat = [{"mode": run.mode, **(run.snapshot.get("settings") or {})} for run in runs]
    differences = [
        {
            "setting": key,
            "values": {run.name: values.get(key) for run, values in zip(runs, flat, strict=True)},
        }
        for key in dict.fromkeys(k for values in flat for k in values)
        if len({json.dumps(values.get(key)) for values in flat}) > 1
    ]
    return Comparison(list(runs), common, differences, notes)


# ------------------------------------------------------------------ rendering


def _table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    widths = [max(len(line[i]) for line in [header, *rows]) for i in range(len(header))]
    lines = [
        "  ".join(cell.ljust(width) for cell, width in zip(line, widths, strict=True)).rstrip()
        for line in [header, *rows]
    ]
    lines.insert(1, "  ".join("-" * width for width in widths))
    return "\n".join(lines)


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _usd(value: float | None) -> str:
    return "n/a" if value is None else f"${value:,.2f}"


def _count(result: IdeaResult, passed: int | None, total: int | None) -> str:
    return "-" if result.status != "scored" or not total else f"{passed}/{total}"


def _totals_rows(summary: Totals) -> list[tuple[str, str]]:
    verified = (
        f"{summary.verified}/{summary.scored} ({_pct(summary.verified_rate)})"
        if summary.scored
        else "n/a"
    )
    per_usd = "n/a" if summary.verified_per_usd is None else f"{summary.verified_per_usd:.2f}"
    unscored = (
        f"{summary.unscored} (${summary.unscored_spend_usd:,.2f} spent)"
        if summary.unscored
        else "0"
    )
    return [
        ("verified deliveries", verified),
        ("verified per dollar", per_usd),
        ("cost per verified delivery", _usd(summary.cost_per_verified)),
        ("cost per scored idea", _usd(summary.cost_per_idea)),
        ("lanes passed", _pct(summary.lane_pass_rate)),
        ("acceptance tests passed", _pct(summary.acceptance_rate)),
        ("fix attempts", str(summary.fix_attempts)),
        ("integrity flags", str(summary.integrity_flags)),
        (
            "mean wall time",
            "n/a" if summary.mean_wall_seconds is None else f"{summary.mean_wall_seconds:,.0f}s",
        ),
        ("unscored ideas", unscored),
    ]


def render_run(record: RunRecord) -> str:
    rows = []
    for r in record.results:
        outcome = (
            "unscored" if r.status != "scored" else "verified" if r.verified else "NOT verified"
        )
        rows.append(
            [
                r.id,
                outcome,
                _count(r, r.lanes_passed, r.lanes_total),
                _count(r, r.acceptance_passed, r.acceptance_total),
                "-" if r.status != "scored" else str(r.fix_attempts),
                "-" if r.status != "scored" else str(r.integrity_flags),
                _usd(r.total_cost_usd),
                f"{r.wall_seconds:,.0f}s",
            ]
        )
    header = ["idea", "outcome", "lanes", "acceptance", "fixes", "flags", "cost", "time"]
    lines = [f"Bench {record.name} ({record.mode})", "", _table(header, rows), ""]
    lines += [f"{label + ':':<28}{value}" for label, value in _totals_rows(totals(record.results))]
    lines += [f"  {r.id}: {r.reason}" for r in record.results if r.status != "scored"]
    unpriced = sum(r.unpriced_calls for r in record.results)
    if unpriced:
        lines.append(f"({unpriced} planning call(s) had no price; their cost is not in the totals)")
    return "\n".join(lines)


def render_comparison(comparison: Comparison) -> str:
    names = [run.name for run in comparison.runs]
    common = ", ".join(comparison.common_ideas) or "none"
    lines = [
        f"Comparing {', '.join(names)} on {len(comparison.common_ideas)} idea(s) scored in every run: {common}",
        "",
    ]
    if comparison.differences:
        lines += [
            "Settings that differ",
            _table(
                ["setting", *names],
                [
                    [d["setting"], *(str(d["values"][n]) for n in names)]
                    for d in comparison.differences
                ],
            ),
            "",
        ]
    else:
        lines += ["The runs use the same settings.", ""]
    per_run = [
        dict(_totals_rows(totals(comparison.results_on_common(run)))) for run in comparison.runs
    ]
    labels = [label for label, _ in _totals_rows(Totals())]
    lines += [
        _table(["result", *names], [[label, *(row[label] for row in per_run)] for label in labels]),
        "",
    ]
    if comparison.common_ideas:
        by_run = {run.name: {r.id: r for r in run.results} for run in comparison.runs}

        def cell(name: str, idea_id: str) -> str:
            r = by_run[name][idea_id]
            return f"{'ok' if r.verified else 'FAIL'}  {_usd(r.total_cost_usd)}"

        lines += [
            _table(
                ["idea", *names],
                [[i, *(cell(n, i) for n in names)] for i in comparison.common_ideas],
            ),
            "",
        ]
    lines += [f"note: {note}" for note in comparison.notes]
    lines.append(
        "Verified per dollar (verified deliveries / spend on scored ideas) is the number that says which configuration is worth its price."
    )
    return "\n".join(lines)


# ------------------------------------------------------------------ dry run


def dry_run(options: BenchOptions) -> dict[str, Any]:
    """What the bench would cost at the catalog's typical-run prices, with no model call and nothing written."""
    with applied_env(options.overrides) as settings:
        per_idea = options.per_idea_usd or settings.implementer_max_total_usd
        estimate = get_catalog().estimate_run(settings.model_profile, configured_choices(settings))
        agents = min(estimate.agents, per_idea)
        per_idea_total = estimate.planning + agents
        total = per_idea_total * len(options.ideas)
        return {
            "dry_run": True,
            "name": options.name,
            "mode": "demo" if settings.demo_mode else "live",
            "settings": settings_snapshot(settings),
            "ideas": [
                {
                    "id": i.id,
                    "platform": i.preferences.get("platform", "any"),
                    "stack_hints": i.preferences.get("stack_hints", ""),
                }
                for i in options.ideas
            ],
            "estimate": {
                "planning_usd": estimate.planning,
                "agents_usd": agents,
                "per_idea_usd": per_idea_total,
                "total_usd": total,
                "per_idea_cap_usd": per_idea,
                "total_cap_usd": options.total_usd,
                "over_cap": options.total_usd is not None and total > options.total_usd,
                "unpriced": estimate.unpriced,
            },
        }


def render_dry_run(report: Mapping[str, Any]) -> str:
    estimate = report["estimate"]
    lines = [
        f"Dry run of bench {report['name']} ({report['mode']}): no model call, nothing written.",
        f"Profile {report['settings']['model_profile']}, harness tuning {report['settings']['harness_tuning']}, "
        f"library {report['settings']['library']}, acceptance {report['settings']['acceptance']}.",
        "",
        _table(
            ["idea", "platform", "stack"],
            [[i["id"], i["platform"], i["stack_hints"] or "-"] for i in report["ideas"]],
        ),
        "",
        f"Typical run per idea: planning {_usd(estimate['planning_usd'])} + agents {_usd(estimate['agents_usd'])} "
        f"(capped at {_usd(estimate['per_idea_cap_usd'])}) = {_usd(estimate['per_idea_usd'])}",
        f"{len(report['ideas'])} idea(s): about {_usd(estimate['total_usd'])} (the catalog's guess, not a quote)",
    ]
    if estimate["unpriced"]:
        lines.append(f"Unpriced roles, left out of the estimate: {', '.join(estimate['unpriced'])}")
    if estimate["total_cap_usd"] is not None:
        over = " - the last ideas would not run" if estimate["over_cap"] else ""
        lines.append(f"--max-total-usd {_usd(estimate['total_cap_usd'])}{over}")
    return "\n".join(lines)


# ------------------------------------------------------------------ the command


def _progress(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def _exit_code(results: Sequence[IdeaResult]) -> int:
    if any(r.status != "scored" for r in results):
        return EXIT_INCOMPLETE
    return EXIT_NOT_VERIFIED if any(not r.verified for r in results) else EXIT_ALL_VERIFIED


def run_command(args: argparse.Namespace) -> int:
    """`idea-to-mvp bench ...`: 0 every idea verified, 2 a delivery failed verification, 3 some ideas unscored
    (run the same call again to retry them), 1 an error."""
    import asyncio

    try:
        root = args.root or get_settings().output_dir / "bench"
        if args.compare:
            return _compare(args, root)
        ideas = load_ideas(
            args.ideas_file or DEFAULT_IDEAS,
            [i for i in args.ideas.split(",") if i] if args.ideas else None,
        )
        overrides = parse_overrides(args.set or [])
        for flag, key in (
            (args.profile, "MODEL_PROFILE"),
            (args.harness_tuning, "HARNESS_TUNING"),
            (args.library, "EXPERIENCE_LIBRARY"),
        ):
            if flag:
                overrides[key] = flag
        options = BenchOptions(
            args.name or "dry-run",
            root,
            ideas,
            overrides,
            args.max_usd_per_idea,
            args.max_total_usd,
            args.live,
        )
        if args.dry_run:
            report = dry_run(options)
            print(json.dumps(report, indent=2) if args.json else render_dry_run(report))
            return EXIT_ALL_VERIFIED
        record = asyncio.run(run_bench(options, progress=_progress))
    except BenchError as exc:
        print(f"idea-to-mvp bench: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if args.json:
        print(
            json.dumps(
                {
                    "name": record.name,
                    "mode": record.mode,
                    "run_dir": str(options.run_dir),
                    "results": [r.to_dict() for r in record.results],
                    "totals": totals(record.results).to_dict(),
                },
                indent=2,
            )
        )
    else:
        print(render_run(record))
    return _exit_code(record.results)


def _compare(args: argparse.Namespace, root: Path) -> int:
    names: list[str] = args.compare
    if len(names) < 2:
        raise BenchError("--compare takes two or more runs: --compare runA runB")
    records = []
    for name in names:
        folder = Path(name) if Path(name).is_dir() else root / name
        record = read_run(folder)
        if record is None:
            raise BenchError(f"no bench run {name!r} (looked in {folder})")
        records.append(record)
    comparison = compare_runs(records)
    print(
        json.dumps(comparison.to_dict(), indent=2) if args.json else render_comparison(comparison)
    )
    return EXIT_ALL_VERIFIED

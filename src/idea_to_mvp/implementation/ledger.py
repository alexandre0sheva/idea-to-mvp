"""The run ledger: one row per agent session, in `<workspace>/.idea-to-mvp/ledger.json`.

Every session the orchestrator starts (a task, a merge resolver, a verification lane, a fix, a harness-tuning
pilot rollout and its quick tests lane) appends a row when it ends, so what a run spent can be traced to the
session that spent it, and the sum of the rows can be checked against the spend that `usage` reports
(`check_ledger`). Harness tuning also records its model calls (two proposals, one composition, the selection) as rows, so
its budget identity can be checked from the ledger alone (`nodes/harness_tuning.py`). Like `progress.json` the file is
git-ignored, left out of the delivery zip, and off limits to agents (the tool guard protects the folder).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypedDict

if TYPE_CHECKING:  # pragma: no cover - typing only
    from idea_to_mvp.usage import UsageRecord

LEDGER_DIR = ".idea-to-mvp"
SessionKind = Literal[
    "task", "resolver", "lane", "fix", "pilot", "pilot_lane", "proposal", "compose", "select"
]
SessionOutcome = Literal["success", "failed", "crashed"]
# The `usage` roles that are agent sessions (planning and panel calls are not in the ledger).
AGENT_USAGE_ROLES = ("implementer", "resolver", "verifier", "fixer", "pilot")
# Model calls the ledger records as well (harness tuning's), and the `usage` roles their spend is recorded under.
CALL_KINDS = ("proposal", "compose", "select")
CALL_USAGE_ROLES = ("harness_editor", "harness_composer", "pilot_selector")
TUNING_SESSION_KINDS = (
    "pilot",
    "pilot_lane",
)  # harness tuning's agent sessions: paid from the whole-run budget

_KINDS = ("task", "resolver", "lane", "fix", "pilot", "pilot_lane", "proposal", "compose", "select")
_OUTCOMES = ("success", "failed", "crashed")
_TOLERANCE_USD = 1e-6
_LOCK = threading.Lock()  # parallel tasks append from one event loop; sync callers may use threads


class LedgerRow(TypedDict):
    kind: SessionKind
    label: str  # also the name of the trajectory file
    task_id: str | None  # the plan task a task, resolver, or pilot session worked on
    iteration: int  # the version being built (1 = v0.1)
    attempt: int  # task / resolver: nth session on that task; lane: verification round; fix: fix attempt; tuning: 1
    model: str
    cost_usd: float
    turns: int
    outcome: SessionOutcome
    session_id: str | None
    trajectory: (
        str  # path relative to the workspace ('' for one kept outside it: the acceptance lane's)
    )
    started_at: float
    ended_at: float


def ledger_path(workspace: Path) -> Path:
    return Path(workspace) / LEDGER_DIR / "ledger.json"


def _valid(entry: Any) -> bool:
    return (
        isinstance(entry, dict)
        and entry.get("kind") in _KINDS
        and isinstance(entry.get("label"), str)
        and isinstance(entry.get("cost_usd"), int | float)
        and isinstance(entry.get("turns"), int)
        and entry.get("outcome") in _OUTCOMES
        and isinstance(entry.get("trajectory"), str)
        and isinstance(entry.get("started_at"), int | float)
        and isinstance(entry.get("ended_at"), int | float)
    )


def load_ledger(workspace: Path) -> list[LedgerRow]:
    """The recorded sessions, oldest first; a missing, corrupt, or malformed file reads as empty."""
    try:
        raw = json.loads(ledger_path(workspace).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, list) or not all(_valid(entry) for entry in raw):
        return []
    return [
        {
            "kind": entry["kind"],
            "label": entry["label"],
            "task_id": entry.get("task_id"),
            "iteration": int(entry.get("iteration") or 1),
            "attempt": int(entry.get("attempt") or 1),
            "model": str(entry.get("model") or ""),
            "cost_usd": float(entry["cost_usd"]),
            "turns": entry["turns"],
            "outcome": entry["outcome"],
            "session_id": entry.get("session_id"),
            "trajectory": entry["trajectory"],
            "started_at": float(entry["started_at"]),
            "ended_at": float(entry["ended_at"]),
        }
        for entry in raw
    ]


def append_row(workspace: Path, row: LedgerRow) -> None:
    """Add one session to the ledger (the file is replaced atomically; a corrupt one starts over)."""
    path = ledger_path(workspace)
    with _LOCK:
        rows = load_ledger(workspace)
        rows.append(row)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".ledger-", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(rows, stream, indent=2)
            os.replace(temp_name, path)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise


def record_call(
    workspace: Path,
    *,
    kind: SessionKind,
    label: str,
    model: str,
    cost_usd: float,
    started_at: float,
    ended_at: float,
) -> LedgerRow:
    """Add a model call (not an agent session: no trajectory, no turns) to the ledger."""
    row: LedgerRow = {
        "kind": kind,
        "label": label,
        "task_id": None,
        "iteration": 1,
        "attempt": 1,
        "model": model,
        "cost_usd": float(cost_usd),
        "turns": 0,
        "outcome": "success",
        "session_id": None,
        "trajectory": "",
        "started_at": started_at,
        "ended_at": max(ended_at, started_at),
    }
    append_row(workspace, row)
    return row


def ledger_total(rows: list[LedgerRow]) -> float:
    """Everything the recorded sessions and calls cost, whatever their outcome."""
    return sum(row["cost_usd"] for row in rows)


_TUNING_CACHE: dict[tuple[str, int], tuple[tuple[int, int, int], float]] = {}


def tuning_spend(workspace: Path, iteration: int) -> float:
    """What harness tuning's agent sessions of a version cost: the task engines deduct it from the whole-run budget.

    The engines ask before every batch and merge, so the sum is kept until the ledger file changes (its inode, size,
    and modification time: `append_row` replaces the file)."""
    path = ledger_path(workspace)
    try:
        info = path.stat()
    except OSError:
        return 0.0
    stamp = (info.st_ino, info.st_size, info.st_mtime_ns)
    key = (str(path), iteration)
    cached = _TUNING_CACHE.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    total = sum(
        row["cost_usd"]
        for row in load_ledger(workspace)
        if row["kind"] in TUNING_SESSION_KINDS and row["iteration"] == iteration
    )
    _TUNING_CACHE[key] = (stamp, total)
    return total


def agent_spend(usage: list[UsageRecord]) -> float:
    """The dollars `usage` attributes to what the ledger records: agent sessions (task, lane, fix, pilot records)
    and harness tuning's model calls."""
    roles = (*AGENT_USAGE_ROLES, *CALL_USAGE_ROLES)
    return sum(record["cost_usd"] or 0.0 for record in usage if record["role"] in roles)


@dataclass(frozen=True)
class LedgerCheck:
    ok: bool
    ledger_usd: float
    usage_usd: float
    message: str = ""  # what differs, empty when `ok`


def check_ledger(workspace: Path, usage: list[UsageRecord]) -> LedgerCheck:
    """Whether the ledger's total equals the agent spend in `usage`: a gap means spend that one of them misses."""
    ledger_usd = ledger_total(load_ledger(workspace))
    usage_usd = agent_spend(usage)
    if abs(ledger_usd - usage_usd) <= _TOLERANCE_USD:
        return LedgerCheck(True, ledger_usd, usage_usd)
    message = f"The run ledger adds up to ${ledger_usd:.2f} but the agent spend recorded in usage is ${usage_usd:.2f}."
    return LedgerCheck(False, ledger_usd, usage_usd, message)


def task_trajectories(workspace: Path) -> dict[str, str]:
    """For each plan task, the trajectory of its latest own session (a resolver or lane is not the task's)."""
    return {
        row["task_id"]: row["trajectory"]
        for row in load_ledger(workspace)
        if row["kind"] == "task" and row["task_id"]
    }

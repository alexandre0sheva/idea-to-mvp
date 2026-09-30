"""Per-workspace implementation progress, so a killed run resumes at the first unfinished task.

Stored in `<workspace>/.idea-to-mvp/progress.json` (git-ignored; the tool guard keeps agents from
writing it). Only `done` tasks are skipped on a re-run; `failed` and `skipped` ones are attempted again.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Literal, TypedDict

from idea_to_mvp.plan import iteration_of

PROGRESS_DIR = ".idea-to-mvp"
_STATUSES = ("done", "failed", "skipped")


class TaskResult(TypedDict):
    task_id: str
    status: Literal["done", "failed", "skipped"]
    summary: str
    cost_usd: float  # everything spent on this task, across attempts
    turns: int
    session_id: str | None
    commit: str | None


def progress_path(workspace: Path) -> Path:
    return Path(workspace) / PROGRESS_DIR / "progress.json"


def _valid(entry: Any) -> bool:
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("task_id"), str)
        and entry.get("status") in _STATUSES
        and isinstance(entry.get("summary"), str)
        and isinstance(entry.get("cost_usd"), int | float)
        and isinstance(entry.get("turns"), int)
    )


def load_progress(workspace: Path) -> dict[str, TaskResult]:
    """Recorded results by task id; a missing, corrupt, or malformed file means no progress."""
    try:
        raw = json.loads(progress_path(workspace).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or not all(_valid(entry) for entry in raw.values()):
        return {}
    return {
        str(task_id): {
            "task_id": entry["task_id"],
            "status": entry["status"],
            "summary": entry["summary"],
            "cost_usd": float(entry["cost_usd"]),
            "turns": entry["turns"],
            "session_id": entry.get("session_id"),
            "commit": entry.get("commit"),
        }
        for task_id, entry in raw.items()
    }


def save_result(workspace: Path, result: TaskResult) -> None:
    """Record one task's result (replacing its earlier entry); the file is replaced atomically."""
    progress = load_progress(workspace)
    progress[result["task_id"]] = result
    path = progress_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".progress-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(progress, stream, indent=2)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def spent_in_iteration(progress: dict[str, TaskResult], iteration: int) -> float:
    """What the tasks of one version (`T..` for v0.1, `I<n>-..` for iteration n) have cost so far."""
    return sum(result["cost_usd"] for task_id, result in progress.items() if iteration_of(task_id) == iteration)

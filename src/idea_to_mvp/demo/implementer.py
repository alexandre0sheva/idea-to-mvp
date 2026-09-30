"""Stand-in for the Claude Agent SDK implementation stage in demo mode.

Writes a tiny but real, dependency-free Python project into the workspace and verifies it by actually
running its unittest suite, so the delivery report and verdict come from a real process.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from idea_to_mvp.implementation.events import ImplEvent, make_event
from idea_to_mvp.implementation.verify import LaneReport, priority_requirements
from idea_to_mvp.plan import load_plan

VERIFY_TIMEOUT_SECONDS = 60


def _idea_from_workspace(workspace: Path) -> str:
    slug = re.sub(r"^\d{8}-\d{6}-\d+-", "", workspace.name)
    return slug.replace("-", " ").strip() or "your product idea"


def demo_implement(workspace: Path, strategy: dict[str, Any]) -> str:
    """Write the demo project and return an implementation summary."""
    workspace = Path(workspace)
    idea = _idea_from_workspace(workspace)
    package = workspace / "demo_app"
    tests = workspace / "tests"
    package.mkdir(parents=True, exist_ok=True)
    tests.mkdir(parents=True, exist_ok=True)

    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "core.py").write_text(
        f'''IDEA = {json.dumps(idea)}


def describe() -> str:
    return f"Demo MVP for: {{IDEA}}"


def add_entry(entries: list[str], entry: str) -> list[str]:
    cleaned = entry.strip()
    if not cleaned:
        raise ValueError("entry must not be empty")
    return [*entries, cleaned]
''',
        encoding="utf-8",
    )
    (package / "__main__.py").write_text(
        "from demo_app.core import describe\n\nprint(describe())\n", encoding="utf-8"
    )
    (tests / "__init__.py").write_text("", encoding="utf-8")
    (tests / "test_core.py").write_text(
        '''import unittest

from demo_app.core import IDEA, add_entry, describe


class CoreTests(unittest.TestCase):
    def test_describe_mentions_the_idea(self):
        self.assertIn(IDEA, describe())

    def test_add_entry_appends_cleaned_text(self):
        self.assertEqual(add_entry(["a"], "  b "), ["a", "b"])

    def test_add_entry_rejects_blank_text(self):
        with self.assertRaises(ValueError):
            add_entry([], "   ")


if __name__ == "__main__":
    unittest.main()
''',
        encoding="utf-8",
    )
    (workspace / "README.md").write_text(
        f"# Demo MVP: {idea}\n\n"
        "Generated in **demo mode** (no models were called): a tiny stand-in project so the whole "
        "pipeline can be tried offline.\n\n"
        "## Run\n\n```bash\npython -m demo_app\n```\n\n"
        "## Test\n\n```bash\npython -m unittest discover -s tests -t .\n```\n",
        encoding="utf-8",
    )

    workstreams = [str(w.get("name") or "workstream") for w in (strategy or {}).get("workstreams") or []]
    lines = [f"Demo mode: built a stand-in project for “{idea}” in `{workspace.name}`."]
    lines += [f"- Workstream `{name}`: completed (simulated)." for name in workstreams]
    lines.append("- Files: `demo_app/`, `tests/test_core.py`, `README.md`.")
    return "\n".join(lines)


def demo_task(workspace: Path, task: Any, emit: Callable[[ImplEvent], None]) -> str:
    """Simulate one plan task: the first task builds the demo project, every task leaves a note file.

    Emits the same kinds of events a real session would, so the live progress UI can be tried offline.
    """
    workspace = Path(workspace)
    if not (workspace / "demo_app").is_dir():
        demo_implement(workspace, {})
        for created in ("demo_app/core.py", "tests/test_core.py", "README.md"):
            emit(make_event("tool", task_id=task.id, label="Write", detail=created))
    emit(make_event("text", task_id=task.id, label="agent", detail=f"Implementing “{task.title}” (simulated)."))
    notes = workspace / "notes"
    notes.mkdir(exist_ok=True)
    (notes / f"{task.id}.md").write_text(
        f"# {task.id}: {task.title}\n\n{task.goal}\n\n"
        + "\n".join(f"- {item}" for item in task.acceptance)
        + "\n",
        encoding="utf-8",
    )
    emit(make_event("tool", task_id=task.id, label="Write", detail=f"notes/{task.id}.md"))
    return f"Demo mode: simulated {task.id} — {task.title}."


def _run(command: list[str], workspace: Path) -> tuple[int, str]:
    """(exit code, output) of a command in the workspace; a timeout is exit code 124."""
    try:
        run = subprocess.run(command, cwd=workspace, capture_output=True, text=True, timeout=VERIFY_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {VERIFY_TIMEOUT_SECONDS}s"
    return run.returncode, (run.stderr or run.stdout).strip()


def demo_verify_lane(workspace: Path, lane: str) -> LaneReport:
    """One verification lane for the demo project, from real checks (the same contract as the agent lanes)."""
    workspace = Path(workspace)
    if lane == "tests":
        command = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."]
        code, output = _run(command, workspace)
        ran = re.search(r"Ran (\d+) tests?", output)
        return LaneReport(
            lane="tests",
            passed=code == 0,
            commands_run=[{"command": "python -m unittest discover -s tests -t .", "exit_code": code}],
            failures=[] if code == 0 else [f"The unittest run failed:\n{output[-1500:]}"],
            summary=f"Ran {ran.group(1) if ran else '0'} tests with `python -m unittest`.",
        )
    if lane == "quality":
        checks = [
            ("python -m compileall -q demo_app tests", [sys.executable, "-m", "compileall", "-q", "demo_app", "tests"]),
            ("python -m demo_app", [sys.executable, "-m", "demo_app"]),  # the smoke probe: it starts and answers
        ]
        runs, failures = [], []
        for label, command in checks:
            code, output = _run(command, workspace)
            runs.append({"command": label, "exit_code": code})
            if code != 0:
                failures.append(f"`{label}` failed:\n{output[-800:]}")
        return LaneReport(
            lane="quality",
            passed=not failures,
            commands_run=runs,
            failures=failures,
            summary="Byte-compiled the demo project and probed `python -m demo_app`.",
        )
    required = priority_requirements((workspace / "PRD.md").read_text(encoding="utf-8") if (workspace / "PRD.md").exists() else "")
    plan = load_plan((workspace / "plan.json").read_text(encoding="utf-8") if (workspace / "plan.json").exists() else None)
    covered = {rid for task in plan.tasks for rid in task.requirement_ids} if plan else set()
    uncovered = [f"{rid} ({priority or 'unprioritised'}): no plan task (and so no test) covers it" for rid, priority in required.items() if rid not in covered]
    return LaneReport(
        lane="requirements",
        passed=not uncovered,
        commands_run=[],
        failures=uncovered,
        summary=f"{len(required) - len(uncovered)} of {len(required)} required requirements are assigned to a task with tests.",
    )


def demo_fix(report: str) -> str:
    return "Demo mode: nothing to fix."

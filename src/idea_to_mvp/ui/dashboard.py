"""The delivery dashboard: one card that says how the run went and what to do with the result.

A verdict card per verification lane, how each PRD requirement fared, what the run cost, and how to run the
project. `render_dashboard` reads the graph state plus the workspace's `plan.json`, `PRD.md`, and `README.md`;
everything it prints from an agent or from those files is escaped.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from idea_to_mvp.implementation.progress import TaskResult, load_progress
from idea_to_mvp.plan import Plan, load_plan, parse_requirements
from idea_to_mvp.ui.components import compact_count, format_elapsed
from idea_to_mvp.usage import summarize_usage

_LANE_ORDER = ("tests", "quality", "requirements")
_RUN_HEADING = re.compile(r"^(#{1,4})\s*(?:run|running|quick\s*start|usage|getting\s*started)\b.*$", re.IGNORECASE)
_HEADING = re.compile(r"^(#{1,6})\s")
_REQUIREMENT_ID = re.compile(r"\bR\d+\b")


def _e(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


# ---------------------------------------------------------------- how to run


def _readme_run_block(readme: str) -> str:
    """The first fenced code block under the README's run / quick start / usage heading."""
    lines = readme.splitlines()
    for index, line in enumerate(lines):
        heading = _RUN_HEADING.match(line)
        if not heading:
            continue
        level = len(heading.group(1))
        block: list[str] = []
        inside = False
        for follower in lines[index + 1 :]:
            if not inside and (other := _HEADING.match(follower)) and len(other.group(1)) <= level:
                break
            if follower.strip().startswith("```"):
                if inside:
                    return "\n".join(block).strip()
                inside = True
                continue
            if inside:
                block.append(follower)
    return ""


def run_instructions(workspace: Path, plan: Plan | None = None) -> list[tuple[str, str]]:
    """(label, commands) to run the project: the plan's install and run commands, else the README's run section."""
    workspace = Path(workspace)
    plan = plan if plan is not None else load_plan(_read(workspace / "plan.json"))
    if plan is not None and plan.commands.run:
        steps = [("Install", plan.commands.install or ""), ("Run", plan.commands.run)]
        return [(label, command) for label, command in steps if command.strip()]
    block = _readme_run_block(_read(workspace / "README.md"))
    return [("README", block)] if block else []


# ------------------------------------------------------------------- lanes


def _lane_card(lane: Mapping[str, Any]) -> str:
    name = str(lane.get("lane") or "")
    passed = bool(lane.get("passed"))
    commands = "".join(
        f"<li><code>{_e(run.get('command', '?'))}</code> (exit {_e(run.get('exit_code', '?'))})</li>"
        for run in lane.get("commands_run") or []
        if isinstance(run, Mapping)
    )
    failures = "".join(f"<li>{_e(failure)}</li>" for failure in lane.get("failures") or [])
    return (
        f"<section class='lane-card {'passed' if passed else 'failed'}' data-lane='{_e(name)}'>"
        f"<header><strong>{_e(name.capitalize())} lane</strong> "
        f"<span class='badge {'passed' if passed else 'failed'}'>{'PASSED' if passed else 'FAILED'}</span></header>"
        f"<p>{_e(lane.get('summary'))}</p>"
        + (f"<p class='lane-label'>Commands</p><ul>{commands}</ul>" if commands else "")
        + (f"<p class='lane-label'>Failures</p><ul class='lane-failures'>{failures}</ul>" if failures else "")
        + "</section>"
    )


# ------------------------------------------------------------- requirements


def _requirement_rows(prd: str, plan: Plan | None, results: Mapping[str, TaskResult], flagged: set[str]) -> str:
    requirements = parse_requirements(prd)
    if not requirements:
        return ""
    covering: dict[str, list[str]] = {rid: [] for rid in requirements}
    for task in plan.tasks if plan else []:
        for rid in task.requirement_ids:
            covering.setdefault(rid, []).append(task.id)
    rows = []
    for rid in sorted(requirements, key=lambda r: int(r[1:])):
        tasks = covering.get(rid, [])
        if not tasks:
            state = "uncovered"
        elif rid in flagged:
            state = "untested"  # planned and built, but the requirements lane found no test for it
        elif any(t in results and results[t]["status"] != "done" for t in tasks):
            state = "incomplete"
        elif all(t in results for t in tasks):
            state = "delivered"
        else:
            state = "planned"
        rows.append(
            f"<tr class='req-row {state}' data-req='{_e(rid)}'><th>{_e(rid)}</th><td>{_e(requirements[rid] or '—')}</td>"
            f"<td>{_e(', '.join(tasks) or '—')}</td><td>{state}</td></tr>"
        )
    return (
        "<table class='req-table'><thead><tr><th>Requirement</th><th>Priority</th><th>Tasks</th><th>Status</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


# ----------------------------------------------------------------- dashboard


def _stat(name: str, label: str, value: str) -> str:
    return f"<div class='stat stat-{name}'><span class='stat-label'>{_e(label)}</span><span class='stat-value'>{_e(value)}</span></div>"


def render_dashboard(state: Mapping[str, Any], *, elapsed: Mapping[str, float] | None = None) -> str:
    """The delivery dashboard for a finished run (`elapsed`: seconds per pipeline step, when known)."""
    verification = state.get("verification") or {}
    passed = bool(verification.get("passed"))
    attempts = int(verification.get("attempts") or 0)
    iteration = int(state.get("iteration") or 1)
    workspace_dir = str(state.get("workspace_dir") or "")
    workspace = Path(workspace_dir) if workspace_dir else None
    plan = load_plan(_read(workspace / "plan.json")) if workspace else None
    results: Mapping[str, TaskResult] = state.get("task_results") or (load_progress(workspace) if workspace else {})
    lanes = [lane for lane in verification.get("lanes") or [] if isinstance(lane, Mapping)]
    lanes.sort(key=lambda lane: _LANE_ORDER.index(lane.get("lane")) if lane.get("lane") in _LANE_ORDER else len(_LANE_ORDER))

    header = (
        f"<header class='dashboard-head'><h3>Delivery report — v0.{iteration}</h3>"
        f"<span class='dashboard-verdict {'passed' if passed else 'failed'}'>"
        f"Verification {'PASSED ✅' if passed else 'FAILED ❌'} after {attempts} fix attempt(s)</span></header>"
    )
    if lanes:
        lane_html = f"<div class='lane-grid'>{''.join(_lane_card(lane) for lane in lanes)}</div>"
    else:
        lane_html = f"<p class='dashboard-note'>{_e(verification.get('report') or 'No verification report.')}</p>"

    flagged: set[str] = set()
    for lane in lanes:
        if lane.get("lane") == "requirements" and not lane.get("passed"):
            flagged.update(_REQUIREMENT_ID.findall(" ".join(str(f) for f in lane.get("failures") or [])))
    table = _requirement_rows(_read(workspace / "PRD.md") if workspace else "", plan, results, flagged)
    requirements_html = f"<h4>Requirements</h4>{table}" if table else ""

    summary = summarize_usage(state.get("usage") or [])
    tasks_total = len(plan.tasks) if plan else len(results)
    tasks_done = sum(1 for task_id, r in results.items() if r["status"] == "done" and (plan is None or any(t.id == task_id for t in plan.tasks)))
    took = sum((elapsed or {}).values())
    stats = "".join(
        [
            _stat("cost", "Agent cost", f"${summary['cost_usd'] or 0.0:.2f}"),
            _stat("tokens", "Model tokens", f"{compact_count(int(summary['total_tokens']))} tokens"),
            _stat("time", "Time", format_elapsed(took) if took > 0 else "—"),
            _stat("tasks", "Tasks", f"{tasks_done} of {tasks_total} tasks"),
        ]
    )

    steps = run_instructions(workspace, plan) if workspace else []
    if steps:
        run_html = "".join(f"<p class='run-label'>{_e(label)}</p><pre class='run-block'>{_e(command)}</pre>" for label, command in steps)
    else:
        run_html = "<p>See <code>README.md</code> in the workspace.</p>"
    archive = str(state.get("delivery_zip") or "")
    where = (
        (f"<p><strong>Project:</strong> <code>{_e(workspace_dir)}</code></p>" if workspace_dir else "")
        + (f"<p><strong>Download:</strong> <code>{_e(archive)}</code></p>" if archive else "")
    )
    return (
        "<div class='dashboard'>"
        f"{header}{lane_html}{requirements_html}<div class='stat-row'>{stats}</div>"
        f"<section class='how-to-run'><h4>How to run</h4>{run_html}</section>{where}</div>"
    )

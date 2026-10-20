"""The delivery dashboard: one card that says how the run went and what to do with the result.

A verdict card per verification lane, what the test-integrity guard flagged, how each PRD requirement fared,
what the run cost, and how to run the project. `render_dashboard` reads the graph state plus the workspace's `plan.json`, `PRD.md`, and `README.md`;
everything it prints from an agent or from those files is escaped.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from idea_to_mvp.implementation.fixes import result_text
from idea_to_mvp.implementation.integrity import CHANGES_FILE
from idea_to_mvp.implementation.progress import TaskResult, load_progress
from idea_to_mvp.plan import Plan, load_plan, parse_requirements
from idea_to_mvp.ui.components import compact_count, format_elapsed
from idea_to_mvp.usage import summarize_usage

_LANE_ORDER = ("tests", "quality", "requirements", "acceptance")
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


def _evidence_line(lane: Mapping[str, Any]) -> str:
    """The tests lane's counts as the orchestrator read them from the test report (else a note that the lane's
    verdict is the agent's word). Empty for the other lanes."""
    if lane.get("lane") == "acceptance":
        return _acceptance_line(lane.get("evidence"))
    if lane.get("lane") != "tests":
        return ""
    evidence = lane.get("evidence")
    if not isinstance(evidence, Mapping):
        return "<p class='lane-evidence unbacked'>Result is agent-reported: no test report backs it.</p>"
    parts = [
        f"{int(evidence.get('passed') or 0)} passed",
        f"{int(evidence.get('failed') or 0) + int(evidence.get('errors') or 0)} failed",
        f"{int(evidence.get('skipped') or 0)} skipped",
    ]
    coverage = evidence.get("coverage_percent")
    if isinstance(coverage, int | float):
        parts.append(f"{coverage:.0f}% coverage")
    return f"<p class='lane-evidence'>{_e(', '.join(parts))} (from the test report)</p>"


def _acceptance_line(evidence: Any) -> str:
    """The held-out suite's counts, and where it is kept (the user may read it; the agents never did)."""
    if not isinstance(evidence, Mapping):
        return ""
    counts = (
        f"{int(evidence.get('passed') or 0)} passed, "
        f"{int(evidence.get('failed') or 0) + int(evidence.get('errors') or 0)} failed, "
        f"{int(evidence.get('skipped') or 0)} skipped"
    )
    where = f" Suite: <code>{_e(evidence.get('suite'))}</code>" if evidence.get("suite") else ""
    return f"<p class='lane-evidence'>{_e(counts)} (held-out acceptance tests, from their report).{where}</p>"


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
        + _evidence_line(lane)
        + (f"<p class='lane-label'>Commands</p><ul>{commands}</ul>" if commands else "")
        + (f"<p class='lane-label'>Failures</p><ul class='lane-failures'>{failures}</ul>" if failures else "")
        + "</section>"
    )


# ------------------------------------------------------------ test integrity

_INTEGRITY_STATUS = {"open": "open", "restored": "restored", "justified": "justified by the agent"}


def _integrity_card(findings: Mapping[str, Any], mode: str, iteration: int) -> str:
    """What the test-integrity guard flagged and what became of it ('' when it was off or did not run)."""
    if mode not in ("block", "warn"):
        return ""
    entries = [finding for finding in findings.values() if isinstance(finding, Mapping)]
    if not entries:
        return "<p class='dashboard-note integrity-note'>Test integrity: no session deleted, renamed, skipped, or weakened a test.</p>"
    open_count = sum(1 for finding in entries if finding.get("status") == "open")
    badge = ("BLOCKED" if mode == "block" else "WARNING") if open_count else "CLEAR"
    items = []
    for finding in entries:
        status = str(finding.get("status") or "open")
        version = f"v0.{int(finding.get('iteration') or 1)} · " if iteration > 1 else ""
        why = f"<br><span class='integrity-reason'>Why: {_e(finding.get('reason'))}</span>" if status == "justified" else ""
        items.append(
            f"<li class='integrity-{_e(status)}'><strong>{_e(_INTEGRITY_STATUS.get(status, status))}</strong> · "
            f"{_e(version)}{_e(finding.get('detail'))}{why}</li>"
        )
    justified = any(finding.get("status") == "justified" for finding in entries)
    note = (
        f"<p class='lane-label'>Justifications are the agents' own words, from <code>{_e(CHANGES_FILE)}</code>: review them.</p>"
        if justified
        else ""
    )
    return (
        f"<section class='lane-card integrity {'failed' if open_count else 'passed'}' data-integrity='{_e(badge.lower())}'>"
        f"<header><strong>Test integrity</strong> <span class='badge {'failed' if open_count else 'passed'}'>{badge}</span></header>"
        f"<p>Tests that agent sessions deleted, renamed, skipped, or weakened ({open_count} open).</p>"
        f"<ul class='integrity-findings'>{''.join(items)}</ul>{note}</section>"
    )


# ------------------------------------------------------------ fix attempts


def _attempt_item(entry: Mapping[str, Any]) -> str:
    """One fix attempt: who ran it, what it changed, what it believed, and how it ended."""
    cost = f" · ${float(entry.get('cost_usd') or 0.0):.2f}" if entry.get("cost_usd") else ""
    result = str(entry.get("result") or "")
    files = ", ".join(f"<code>{_e(name)}</code>" for name in entry.get("files") or [])
    notes = "".join(
        f"<li><em>{_e(note.get('verdict'))}</em> · {_e(note.get('hypothesis'))}"
        f"<br><span class='integrity-reason'>Evidence: {_e(note.get('evidence'))}</span></li>"
        for note in entry.get("notes") or []
        if isinstance(note, Mapping)
    )
    extra = ""
    if entry.get("kind") == "environment":
        extra = f"<p>The session found that no change to the project can fix this: {_e(entry.get('summary') or 'see its folder')}</p>"
    if entry.get("notes_problem"):
        extra += f"<p class='lane-label'>{_e(entry['notes_problem'])}</p>"
    return (
        f"<li class='fix-attempt {_e(result or 'unverified')}'><strong>Attempt {_e(entry.get('attempt'))}</strong> · "
        f"{_e(entry.get('model') or 'unknown model')}{_e(cost)} · {_e(result_text(entry))}"
        f"<br>{'Changed: ' + files if files else 'Changed nothing.'}"
        f"{f'<ul>{notes}</ul>' if notes else ''}{extra}</li>"
    )


def _fix_history_card(verification: Mapping[str, Any]) -> str:
    """The fix attempts of this version, oldest first ('' when there were none). The text of each is an agent's."""
    entries = [entry for entry in verification.get("history") or [] if isinstance(entry, Mapping)]
    if not entries:
        return ""
    passed = bool(verification.get("passed"))
    environment = str(verification.get("environment") or "")
    stopped = (
        f"<p class='lane-label'>Stopped: the environment, not the project, is at fault. {_e(environment)}</p>" if environment else ""
    )
    return (
        f"<section class='lane-card fix-history {'passed' if passed else 'failed'}' data-fix-attempts='{len(entries)}'>"
        f"<header><strong>Fix attempts</strong> <span class='badge {'passed' if passed else 'failed'}'>{len(entries)}</span></header>"
        "<p>Each attempt read the folders of the earlier ones in <code>.idea-to-mvp/fix/</code>.</p>"
        f"<ol class='fix-attempts'>{''.join(_attempt_item(entry) for entry in entries)}</ol>{stopped}</section>"
    )


# ------------------------------------------------------------- requirements


def _requirement_rows(
    prd: str, plan: Plan | None, results: Mapping[str, TaskResult], flagged: set[str], acceptance: Mapping[str, str] | None = None
) -> str:
    """One row per PRD requirement; with an acceptance lane, also how its held-out tests did (`acceptance`)."""
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
        held_out = ""
        if acceptance is not None:
            verdict = str(acceptance.get(rid) or "no test")
            held_out = f"<td class='req-acceptance {_e(verdict.replace(' ', '-'))}'>{_e(verdict)}</td>"
        rows.append(
            f"<tr class='req-row {state}' data-req='{_e(rid)}'><th>{_e(rid)}</th><td>{_e(requirements[rid] or '—')}</td>"
            f"<td>{_e(', '.join(tasks) or '—')}</td><td class='req-status'>{state}</td>{held_out}</tr>"
        )
    acceptance_head = "<th>Acceptance</th>" if acceptance is not None else ""
    return (
        "<table class='req-table'><thead><tr><th>Requirement</th><th>Priority</th><th>Tasks</th><th>Status</th>"
        f"{acceptance_head}</tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


# ----------------------------------------------------------------- dashboard


def _stat(name: str, label: str, value: str) -> str:
    return f"<div class='stat stat-{name}'><span class='stat-label'>{_e(label)}</span><span class='stat-value'>{_e(value)}</span></div>"


def _planning_cost(summary: Mapping[str, Any]) -> str:
    planning = summary["planning_cost_usd"]
    if planning is None:
        return "unpriced" if summary["unpriced_calls"] else "$0.00"
    return f"${planning:.2f}" + (" + unpriced calls" if summary["unpriced_calls"] else "")


def _estimate_stat(estimate: Mapping[str, Any], summary: Mapping[str, Any], iteration: int) -> str:
    """The implement gate's estimate against what the first version's agents spent ('' for later versions)."""
    if not estimate or iteration != 1:
        return ""
    low, high = estimate.get("low"), estimate.get("high")
    if low is None or high is None:
        return _stat("estimate", "Estimate vs actual", f"estimate unpriced · ${summary['agent_cost_usd']:.2f} actual")
    actual = float(summary["agent_cost_usd"])
    verdict = "under" if actual < low else "over" if actual > high else "within"
    tuned = " (incl. harness tuning)" if estimate.get("harness_tuning") else ""
    return _stat(
        "estimate", "Estimate vs actual", f"${low:.2f}–${high:.2f} estimated{tuned} · ${actual:.2f} actual ({verdict})"
    )


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
    lane_html += _integrity_card(state.get("integrity_findings") or {}, str(verification.get("integrity") or ""), iteration)
    lane_html += _fix_history_card(verification)

    flagged: set[str] = set()
    for lane in lanes:
        if lane.get("lane") == "requirements" and not lane.get("passed"):
            flagged.update(_REQUIREMENT_ID.findall(" ".join(str(f) for f in lane.get("failures") or [])))
    acceptance = next(
        (lane.get("evidence") for lane in lanes if lane.get("lane") == "acceptance" and isinstance(lane.get("evidence"), Mapping)),
        None,
    )
    held_out = acceptance.get("requirements") if isinstance(acceptance, Mapping) else None
    table = _requirement_rows(
        _read(workspace / "PRD.md") if workspace else "", plan, results, flagged, held_out if isinstance(held_out, Mapping) else None
    )
    requirements_html = f"<h4>Requirements</h4>{table}" if table else ""

    summary = summarize_usage(state.get("usage") or [])
    tasks_total = len(plan.tasks) if plan else len(results)
    tasks_done = sum(1 for task_id, r in results.items() if r["status"] == "done" and (plan is None or any(t.id == task_id for t in plan.tasks)))
    took = sum((elapsed or {}).values())
    estimate_html = _estimate_stat(state.get("cost_estimate") or {}, summary, iteration)
    stats = "".join(
        [
            _stat("planning", "Planning cost", _planning_cost(summary)),
            _stat("cost", "Agent cost", f"${summary['agent_cost_usd']:.2f}"),
            estimate_html,
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

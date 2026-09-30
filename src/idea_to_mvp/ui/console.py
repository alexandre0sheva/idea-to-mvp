"""The implementation view: a task board, a live console, and a cost meter.

Pure renderers over data (no Gradio, no I/O), styled by `ui/theme.py`. Everything an agent wrote (task
titles, summaries, tool details, diff stats) is escaped: it is untrusted text.
"""

from __future__ import annotations

import html
import time
from collections.abc import Collection, Mapping, Sequence
from typing import Any

from idea_to_mvp.implementation.events import ImplEvent
from idea_to_mvp.implementation.progress import TaskResult

COLUMNS: tuple[tuple[str, str], ...] = (
    ("pending", "Pending"),
    ("running", "Running"),
    ("done", "Done"),
    ("failed", "Failed"),
)
MAIN_BRANCH = "main"


def _e(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def task_state(task_id: str, results: Mapping[str, TaskResult], running: Collection[str]) -> str:
    """The board column of a task.

    Results come from disk and `running` from the event stream, which can lag behind the disk, so a finished
    task stays done even if its start event is only now being seen. A task that is running again after a
    failure is running (a failed result is not final).
    """
    result = results.get(task_id)
    if result is not None and result["status"] == "done":
        return "done"
    if task_id in running:
        return "running"
    if result is None:
        return "pending"
    return "failed"


def _text_block(text: str) -> str:
    return _e(text.strip()).replace("\n", "<br>")


def _card(
    task: Mapping[str, Any],
    state: str,
    result: TaskResult | None,
    *,
    parallel: bool,
    diff: str,
) -> str:
    task_id = str(task.get("id") or "")
    meta = [f"<span class='chip'>{_e(task.get('workstream'))}</span>", f"<span class='chip'>{_e(f'task/{task_id}' if parallel else MAIN_BRANCH)}</span>"]
    badge = ""
    body = ""
    if result is not None:
        if result["cost_usd"] > 0:
            meta.append(f"<span class='task-cost'>${result['cost_usd']:.2f}</span>")
        if result["turns"]:
            meta.append(f"<span class='task-turns'>{int(result['turns'])} turns</span>")
        if result["commit"]:
            meta.append(f"<code>{_e(result['commit'])}</code>")
        if result["status"] == "skipped":
            badge = "<span class='badge skipped'>skipped</span>"
        summary = result["summary"].strip()
        parts = []
        if summary:
            parts.append(f"<div class='task-summary'>{_text_block(summary)}</div>")
        if diff.strip():
            parts.append(f"<pre class='diffstat'>{_e(diff.rstrip())}</pre>")
        if parts:
            body = "<details><summary>Summary and changes</summary>" + "".join(parts) + "</details>"
    elif state == "pending" and task.get("depends_on"):
        meta.append(f"<span class='task-deps'>after {_e(', '.join(str(d) for d in task['depends_on']))}</span>")
    return (
        f"<article class='task-card {state}' data-task='{_e(task_id)}'>"
        f"<header><strong>{_e(task_id)}</strong> <span class='task-title'>{_e(task.get('title'))}</span> {badge}</header>"
        f"<div class='task-meta'>{' '.join(meta)}</div>{body}</article>"
    )


def render_task_board(
    plan: Mapping[str, Any],
    results: Mapping[str, TaskResult],
    running: Collection[str],
    *,
    diffs: Mapping[str, str] | None = None,
    parallel: bool = False,
) -> str:
    """Kanban of the plan's tasks: pending | running | done | failed (skipped tasks count as failed).

    `diffs` maps a commit to its `git diff --stat`; `parallel` says tasks run on their own `task/<id>` branch
    rather than on the main line.
    """
    tasks = [task for task in plan.get("tasks") or [] if isinstance(task, Mapping)]
    if not tasks:
        return "<p class='board-empty'>The task board appears once the blueprint's plan is being built.</p>"
    diffs = diffs or {}
    grouped: dict[str, list[str]] = {name: [] for name, _ in COLUMNS}
    for task in tasks:
        task_id = str(task.get("id") or "")
        state = task_state(task_id, results, running)
        result = results.get(task_id)
        commit = result["commit"] if result and result["commit"] else ""
        grouped[state].append(_card(task, state, result, parallel=parallel, diff=diffs.get(commit, "") if commit else ""))
    columns = "".join(
        f"<section class='board-col' data-col='{name}'><h4>{label} ({len(grouped[name])})</h4>{''.join(grouped[name])}</section>"
        for name, label in COLUMNS
    )
    return f"<div class='task-board'>{columns}</div>"


def running_tasks(events: Sequence[ImplEvent]) -> set[str]:
    """Plan tasks that have started and not ended in the event stream (verification lanes are not tasks)."""
    running: set[str] = set()
    for event in events:
        task_id = event["task_id"]
        if not task_id or task_id.startswith("verify:"):
            continue
        if event["kind"] == "task_start":
            running.add(task_id)
        elif event["kind"] == "task_end":
            running.discard(task_id)
    return running


# ------------------------------------------------------------------- console


def _line(event: ImplEvent) -> str:
    kind = event["kind"]
    who = event["task_id"] or ""
    stamp = time.strftime("%H:%M:%S", time.localtime(event["ts"]))
    if kind == "tool":
        what = f"<span class='label'>{_e(event['label'])}</span> {_e(event['detail'])}"
    elif kind == "text":
        what = _e(event["detail"])
    elif kind == "task_start":
        what = f"▶ started: {_e(event['label'])} {_e(event['detail'])}".rstrip()
    elif kind == "task_end":
        what = f"■ {_e(event['label'])}: {_e(event['detail'])}"
    else:  # cost
        cost = f"${event['cost_usd']:.2f}" if event["cost_usd"] is not None else ""
        what = f"{cost} · {_e(event['label'])} {_e(event['detail'])}".strip(" ·")
    return (
        f"<div class='console-line {kind}'><span class='ts'>{stamp}</span>"
        f"<span class='who'>{_e(who)}</span><span class='what'>{what}</span></div>"
    )


def render_console(events: Sequence[ImplEvent], limit: int = 200) -> str:
    """The live log, oldest first. It is collapsible, and the scroll area is anchored to its bottom, so the
    newest line stays in view as events arrive."""
    shown = list(events)[-limit:] if limit > 0 else []
    if not shown:
        body = "<div class='console-line'><span class='what'>Waiting for the first event…</span></div>"
        title = "Live log"
    else:
        body = "".join(_line(event) for event in shown)
        title = f"Live log ({len(events)} events)" if len(shown) == len(events) else f"Live log (last {len(shown)} of {len(events)})"
    return (
        f"<details class='console' open><summary>{title}</summary>"
        f"<div class='console-scroll' role='log' aria-live='polite'><div class='console-lines'>{body}</div></div></details>"
    )


# --------------------------------------------------------------------- meter

_WARN_SHARE = 0.8


def render_cost_meter(spent: float, budget: float) -> str:
    """Agent spend against the run's budget; turns amber near the cap and red beyond it."""
    if budget <= 0:
        return f"<div class='cost-meter'><span class='cost-text'>${spent:.2f} spent</span></div>"
    share = spent / budget
    state = " over" if share >= 1 else " warn" if share >= _WARN_SHARE else ""
    percent = min(100, round(share * 100))
    return (
        f"<div class='cost-meter{state}' role='meter' aria-valuemin='0' aria-valuemax='{budget:.2f}' aria-valuenow='{spent:.2f}'>"
        f"<div class='cost-bar'><div class='cost-fill' style='width: {percent}%'></div></div>"
        f"<span class='cost-text'>${spent:.2f} of ${budget:.2f}</span></div>"
    )

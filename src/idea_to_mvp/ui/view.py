"""Pure projection of graph state onto what the UI shows. No Gradio imports.

The chat transcript, the UI mode, and the status line are all *derived* from the checkpointed graph
state (plus the interrupt the graph is paused at), so the UI can never drift from the pipeline and
any session can be rebuilt after a restart.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from langchain_core.messages import AIMessage

from idea_to_mvp.implementation.events import ImplEvent
from idea_to_mvp.nodes.common import display_speaker_name
from idea_to_mvp.text_utils import normalize_message_content
from idea_to_mvp.ui.components import canonical_stage

MODE_IDEA = "idea"
MODE_ANSWERS = "answers"
MODE_ARCH_CHOICE = "arch_choice"
MODE_PLAN_GATE = "plan_gate"
MODE_IMPL_GATE = "implement_gate"
MODE_ITERATE_GATE = "iterate_gate"  # after a delivery: request changes for the next version, or finish
MODE_DONE = "done"
MODE_INTERRUPTED = "interrupted"  # paused mid-run (crash/stop), not at a gate: can be continued

GATE_MODES: dict[str, str] = {
    "answers": MODE_ANSWERS,
    "arch_choice": MODE_ARCH_CHOICE,
    "plan_gate": MODE_PLAN_GATE,
    "implement_gate": MODE_IMPL_GATE,
    "iterate_gate": MODE_ITERATE_GATE,
}

ARCH_CHOICE_A = "Option A — fast & maintainable"
ARCH_CHOICE_B = "Option B — performance & scale"
PLAN_CHOICE_GENERATE = "Generate the execution pack"
PLAN_CHOICE_SKIP = "Skip for now"
IMPL_CHOICE_START = "Start implementation"
IMPL_CHOICE_SKIP = "Stop here (blueprint only)"
ITERATE_CHOICE_CHANGES = "Request changes (next version)"
ITERATE_CHOICE_DONE = "Finish here"

# Order in which `state["stage"]` advances; used to tell whether a gate has been decided.
_STAGES = [
    "discussion", "summary", "answers", "architecture", "arch_choice", "strategy",
    "plan_gate", "plan_bundle", "implement_gate", "implementation", "verification", "report", "done",
]  # fmt: skip

# Kinds whose entries are messages the user sent (rendered as user bubbles).
USER_KINDS = frozenset(
    {"idea", "user_answers", "arch_decision", "planner_decision", "implement_decision", "change_request"}
)

# Graph node about to run -> (speaker, message) for the in-progress indicator.
_RUNNING: dict[str, tuple[str, str]] = {
    "summarizer": ("Summarizer", "Synthesizing the discussion into a summary and MVP questions..."),
    "architect": ("Architect", "Turning your answers into two architecture options..."),
    "strategy": ("Strategy", "Choosing the agent execution strategy..."),
    "plan_bundle": (
        "Planner",
        "Generating the blueprint pack: PRD, architecture doc, plan, and subagent definitions...",
    ),
    "prepare_workspace": ("Workspace", "Preparing the project workspace (copying the pack, initialising git)..."),
    "implementer": ("Implementer", "Implementation agents are building the project. This can take a while..."),
    "verify": ("Verifier", "Verifying in three lanes: tests, quality, and requirement coverage..."),
    "fix": ("Fixer", "Fixing what verification found..."),
    "delivery_report": ("Delivery", "Assembling the delivery report..."),
    "change_planner": ("Change planner", "Planning your changes as new tasks..."),
}


@dataclass(frozen=True)
class ViewEntry:
    kind: str
    speaker: str
    content: str
    data: Any = None  # typed payload for richer rendering (e.g. question items)


def _reached(values: Mapping[str, Any], stage: str) -> bool:
    current = str(values.get("stage") or "discussion")
    if current not in _STAGES:
        return False
    return _STAGES.index(current) >= _STAGES.index(stage)


# Graph nodes that wait for the user: the time until the run continues from one of them is not work.
GATE_NODES = frozenset({"collect_answers", "arch_choice", "plan_gate", "implement_gate", "iterate_gate"})


@dataclass(frozen=True)
class StageMark:
    """One checkpoint of the run: when it was written, the stage it reached, and whether the graph then
    waited for the user."""

    at: float
    stage: str
    at_gate: bool


def stage_marks(snapshots: Iterable[Any]) -> list[StageMark]:
    """Marks in chronological order from a thread's checkpoint history (`aget_state_history` yields newest
    first). Checkpoints without a stage or a timestamp are skipped."""
    marks: list[StageMark] = []
    for snapshot in reversed(list(snapshots)):
        stage = (getattr(snapshot, "values", None) or {}).get("stage")
        created = getattr(snapshot, "created_at", None)
        if not stage or not created:
            continue
        try:
            at = datetime.fromisoformat(str(created)).timestamp()
        except ValueError:
            continue
        marks.append(StageMark(at, str(stage), bool(GATE_NODES & set(getattr(snapshot, "next", None) or ()))))
    return marks


def stage_elapsed(marks: Sequence[StageMark], *, now: float | None = None, active: str | None = None) -> dict[str, float]:
    """Seconds of work per pipeline step, derived from checkpoint times (nothing is stored for it).

    Each checkpoint is credited with the time since the previous one, unless the graph was waiting at a
    gate then. With `now` and `active`, the step that is running gets the time since the last checkpoint.
    """
    elapsed: dict[str, float] = {}
    for previous, current in zip(marks, marks[1:], strict=False):
        if previous.at_gate:
            continue
        key = canonical_stage(current.stage)
        elapsed[key] = elapsed.get(key, 0.0) + max(0.0, current.at - previous.at)
    if now is not None and active and marks and not marks[-1].at_gate:
        key = canonical_stage(active)
        elapsed[key] = elapsed.get(key, 0.0) + max(0.0, now - marks[-1].at)
    return elapsed


def stage_statuses(values: Mapping[str, Any]) -> dict[str, str]:
    """Steps whose status is not just their position: a verification that did not pass."""
    verification = values.get("verification") or {}
    if str(verification.get("report") or "").strip() and not verification.get("passed"):
        return {"verification": "failed"}
    return {}


def with_notes(text: str, notes: str) -> str:
    return f"{text} — {notes}" if notes else text


def strategy_markdown(strategy: Mapping[str, Any]) -> str:
    lines = [
        f"**Execution mode:** `{strategy.get('mode') or 'unknown'}`",
        "",
        str(strategy.get("reasoning") or "").strip(),
        "",
        "**Workstreams:**",
    ]
    for workstream in strategy.get("workstreams") or []:
        lines.append(f"- `{workstream.get('name') or 'workstream'}` — {workstream.get('focus') or ''}")
    return "\n".join(lines).strip()


def panel_running(values: Mapping[str, Any]) -> tuple[str, str]:
    """The in-progress indicator for the panel: parallel openings first, then one speaker at a time."""
    if values.get("panel_mode", "moderated") == "moderated" and not values.get("turn_count"):
        return "Panel", "PM, Tech Lead, and Skeptic are writing their opening statements in parallel..."
    speaker = str(values.get("next_speaker") or "PM")
    return speaker, f"{speaker} is drafting the next panel turn..."


_CONSOLE_ACTIONS = 8
_VERIFY_PREFIX = "verify:"  # task ids of the verification lanes and the fix session (implementation/verify.py)


def _who(task_id: str, label: str) -> str:
    """How a working unit is named: a plan task by id and title, a verification lane by its title."""
    return label if task_id.startswith(_VERIFY_PREFIX) else f"{task_id} {label}"


def _verb(task_ids: Sequence[str]) -> str:
    if task_ids and all(task_id == f"{_VERIFY_PREFIX}fix" for task_id in task_ids):
        return "Fixing"
    if task_ids and all(task_id.startswith(_VERIFY_PREFIX) for task_id in task_ids):
        return "Verifying"
    return "Implementing"


def implementation_progress(events: Sequence[ImplEvent], *, total_budget: float) -> tuple[str, str]:
    """(status line, console text) for a running implementation, from the live event stream.

    Several tasks can run at once (parallel worktrees), so every task that has started and not ended is
    shown. Transient by nature (events are not part of the checkpoint); once the step finishes the result
    is shown from `task_results` and the implementation log instead.
    """
    finished: list[str] = []
    running: dict[str, ImplEvent] = {}
    actions: dict[str, list[ImplEvent]] = {}
    last_tool: ImplEvent | None = None
    spent = 0.0
    last_task = ""
    last_label = ""
    for event in events:
        task_id = event["task_id"] or ""
        if event["kind"] == "cost":
            spent += event["cost_usd"] or 0.0
        elif event["kind"] == "task_start":
            running[task_id] = event
            actions[task_id] = []
            last_task, last_label = task_id, event["label"]
        elif event["kind"] == "task_end":
            finished.append(f"{_who(task_id, event['label'])}: {event['detail']}")
            running.pop(task_id, None)
            actions.pop(task_id, None)
            last_task, last_label = task_id or last_task, event["label"]
        elif event["kind"] in ("tool", "text") and task_id in running:
            actions[task_id].append(event)
            if event["kind"] == "tool":
                last_tool = event
    if not running and not finished:
        return "", ""
    money = f"${spent:.2f} of ${total_budget:.2f}"
    if not running:
        named = last_label if last_task.startswith(_VERIFY_PREFIX) else last_task
        return f"{_verb([last_task])}: {named} finished · {money}", "\n".join(finished[-_CONSOLE_ACTIONS:])
    names = [(tid, start["label"], start["detail"]) for tid, start in running.items()]
    verb = _verb([tid for tid, _, _ in names])
    if len(names) == 1:
        tid, label, position = names[0]
        head = f"{verb} {tid}" if not tid.startswith(_VERIFY_PREFIX) else verb
        status = f"{head}{f' ({position})' if position else ''}: {label}"
    else:
        noun = "lanes" if verb == "Verifying" else "tasks"
        status = f"{verb} {len(names)} {noun} in parallel ({', '.join(_who(tid, label) for tid, label, _ in names)})"
    tools = [a for a in actions[names[-1][0]] if a["kind"] == "tool"] if len(names) == 1 else []
    latest = tools[-1] if tools else (last_tool if len(names) > 1 else None)
    if latest is not None and latest["task_id"] in running:
        status += f" · {latest['label']} {latest['detail']}".rstrip()
    per_task = _CONSOLE_ACTIONS if len(names) == 1 else 4
    lines = list(finished[-3:])
    for tid, label, _ in names:
        lines.append(f"{_who(tid, label)} — working")
        lines += [
            f"  {a['label']} {a['detail']}".rstrip() if a["kind"] == "tool" else f"  {a['detail']}"
            for a in actions[tid][-per_task:]
        ]
    return f"{status} · {money}", "\n".join(lines)


def running_from_state(values: Mapping[str, Any], next_nodes: Sequence[str]) -> tuple[str, str] | None:
    """The (speaker, message) of the step that is about to run, if it is a working step."""
    for node in next_nodes:
        if node == "panel":
            return panel_running(values)
        if node in _RUNNING:
            return _RUNNING[node]
    return None


def mode_from_state(
    values: Mapping[str, Any],
    pending_interrupt: Mapping[str, Any] | None,
    *,
    next_nodes: Sequence[str] = (),
) -> str:
    kind = (pending_interrupt or {}).get("kind")
    if kind in GATE_MODES:
        return GATE_MODES[str(kind)]
    if not values.get("user_idea"):
        return MODE_IDEA
    return MODE_INTERRUPTED if next_nodes else MODE_DONE


def status_from_state(
    values: Mapping[str, Any],
    mode: str,
    running: tuple[str, str] | None = None,
) -> str:
    if running is not None:
        if running[0] in ("PM", "Tech Lead", "Skeptic"):
            done = int(values.get("turn_count") or 0)
            total = int(values.get("max_rounds") or 0)
            return f"Turn {done}/{total} complete. Next: {running[0]}." if done else "Running panel discussion..."
        return running[1]
    return {
        MODE_IDEA: "",
        MODE_ANSWERS: "Answer the MVP decision questions to continue.",
        MODE_ARCH_CHOICE: "Pick the architecture option to target. Notes are optional.",
        MODE_PLAN_GATE: "Choose whether to generate the execution pack. Notes are optional.",
        MODE_IMPL_GATE: "Decide whether to start the implementation stage. This spends API tokens.",
        MODE_ITERATE_GATE: "Describe the changes you want for the next version, or finish here. Changes spend API tokens.",
        MODE_INTERRUPTED: "This run was interrupted before it finished. Press Continue to resume from the last checkpoint.",
        MODE_DONE: "Pipeline finished. Use Clear to start a fresh session, or describe a new idea.",
    }.get(mode, "")


def stage_for_view(values: Mapping[str, Any], mode: str) -> str:
    """Stage name for the progress tracker."""
    gate_stage = {
        MODE_ANSWERS: "answers",
        MODE_ARCH_CHOICE: "arch_choice",
        MODE_PLAN_GATE: "plan_gate",
        MODE_IMPL_GATE: "implement_gate",
        MODE_ITERATE_GATE: "done",
    }
    if mode in gate_stage:
        return gate_stage[mode]
    if mode == MODE_IDEA:
        return "discussion"
    if mode == MODE_DONE:
        return "done"
    return str(values.get("stage") or "discussion")


def transcript_from_state(
    values: Mapping[str, Any],
    pending_interrupt: Mapping[str, Any] | None,
    *,
    running: tuple[str, str] | None = None,
    pending_user: ViewEntry | None = None,
    error: str | None = None,
) -> list[ViewEntry]:
    """Everything the chat shows, in pipeline order.

    `pending_interrupt` is the payload of the gate the graph is paused at (adds that gate's card).
    `pending_user` / `running` / `error` are transient overlays for the moment right after a submit:
    the user's message before the graph has recorded it, the step in progress, and a failure notice.
    """
    entries: list[ViewEntry] = []
    idea = str(values.get("user_idea") or "").strip()
    if idea:
        entries.append(ViewEntry("idea", "You", idea))

    for message in values.get("discussion_history") or []:
        if isinstance(message, AIMessage):
            speaker = display_speaker_name(message.name) if message.name else "Panelist"
            entries.append(ViewEntry("discussion", speaker, normalize_message_content(message)))

    convergence = values.get("convergence") or {}
    if convergence.get("converged") and (reason := str(convergence.get("reason") or "").strip()):
        entries.append(ViewEntry("moderator", "Moderator", reason))

    if summary := str(values.get("summary") or "").strip():
        entries.append(ViewEntry("summary", "Summary", summary))

    if questions := [str(q) for q in values.get("generated_questions") or []]:
        entries.append(ViewEntry("questions", "Questions", "\n".join(questions), data=values.get("questions")))

    if answers := str(values.get("user_answers") or "").strip():
        entries.append(ViewEntry("user_answers", "You", answers))

    if architecture := str(values.get("architecture") or "").strip():
        entries.append(ViewEntry("architect", "Architect", architecture))

    choice = values.get("arch_choice") or {}
    if choice.get("option"):
        text = with_notes(f"Architecture choice: Option {choice['option']}", str(choice.get("notes") or ""))
        entries.append(ViewEntry("arch_decision", "You", text))

    strategy = values.get("execution_strategy") or {}
    if strategy.get("mode"):
        entries.append(ViewEntry("strategy", "Strategy", strategy_markdown(strategy)))

    plan_decision = values.get("plan_decision") or {}
    generated = bool(plan_decision.get("generate"))
    if _reached(values, "plan_bundle"):
        label = PLAN_CHOICE_GENERATE if generated else PLAN_CHOICE_SKIP
        entries.append(ViewEntry("planner_decision", "You", with_notes(label, str(plan_decision.get("notes") or ""))))

    if bundle := str(values.get("project_bundle_summary") or values.get("project_bundle_dir") or "").strip():
        entries.append(ViewEntry("project_bundle", "Planner", bundle))

    implement_decision = values.get("implement_decision") or {}
    if generated and _reached(values, "implementation"):
        label = IMPL_CHOICE_START if implement_decision.get("implement") else IMPL_CHOICE_SKIP
        notes = str(implement_decision.get("notes") or "")
        entries.append(ViewEntry("implement_decision", "You", with_notes(label, notes)))

    for number, request in enumerate(values.get("change_requests") or [], start=2):
        entries.append(ViewEntry("change_request", "You", f"Change request (v0.{number}): {request}"))

    if log := str(values.get("implementation_log") or "").strip():
        workspace = str(values.get("workspace_dir") or "").strip()
        content = f"**Workspace:** `{workspace}`\n\n{log}" if workspace else log
        entries.append(ViewEntry("implementation", "Implementer", content))

    verification = values.get("verification") or {}
    delivered = bool(str(values.get("delivery_report") or "").strip())
    if str(verification.get("report") or "").strip() and not delivered:  # the dashboard carries the lanes once delivered
        verdict = "passed ✅" if verification.get("passed") else "failed ❌"
        attempts = int(verification.get("attempts") or 0)
        content = (
            f"Verification {verdict} after {attempts} fix attempt(s).\n\n{str(verification['report']).strip()}"
        )
        entries.append(ViewEntry("verification", "Verifier", content))

    if report := str(values.get("delivery_report") or "").strip():
        entries.append(ViewEntry("delivery_report", "Delivery", report, data=values))  # rendered as the dashboard

    entries.extend(_gate_card(pending_interrupt) if pending_user is None else [])

    if pending_user is not None:
        entries.append(pending_user)
    if running is not None:
        entries.append(ViewEntry("thinking", running[0], running[1]))
    if error:
        entries.append(ViewEntry("system", "Orchestrator error", error))
    return entries


def _gate_card(pending_interrupt: Mapping[str, Any] | None) -> list[ViewEntry]:
    payload = pending_interrupt or {}
    kind = payload.get("kind")
    question = str(payload.get("question") or "").strip()
    if kind == "arch_choice":
        return [
            ViewEntry(
                "arch_choice", "Architect", question or "Which architecture option should we target?"
            )
        ]
    if kind == "plan_gate":
        return [ViewEntry("planner_offer", "Planner", question or "Generate the execution pack?")]
    if kind == "implement_gate":
        cards = [ViewEntry("implement_gate", "Planner", question or "Start the implementation stage now?")]
        if warning := str(payload.get("warning") or "").strip():
            cards.append(ViewEntry("warning", "Security warning", warning))
        return cards
    if kind == "iterate_gate":
        return [ViewEntry("iterate_gate", "Delivery", question or "Want changes for the next version?")]
    return []  # the answers gate is the questions entry itself

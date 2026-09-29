"""Pure projection of graph state onto what the UI shows. No Gradio imports.

The chat transcript, the UI mode, and the status line are all *derived* from the checkpointed graph
state (plus the interrupt the graph is paused at), so the UI can never drift from the pipeline and
any session can be rebuilt after a restart.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage

from idea_to_mvp.nodes.common import display_speaker_name
from idea_to_mvp.text_utils import normalize_message_content

MODE_IDEA = "idea"
MODE_ANSWERS = "answers"
MODE_ARCH_CHOICE = "arch_choice"
MODE_PLAN_GATE = "plan_gate"
MODE_IMPL_GATE = "implement_gate"
MODE_DONE = "done"
MODE_INTERRUPTED = "interrupted"  # paused mid-run (crash/stop), not at a gate: can be continued

GATE_MODES: dict[str, str] = {
    "answers": MODE_ANSWERS,
    "arch_choice": MODE_ARCH_CHOICE,
    "plan_gate": MODE_PLAN_GATE,
    "implement_gate": MODE_IMPL_GATE,
}

ARCH_CHOICE_A = "Option A — fast & maintainable"
ARCH_CHOICE_B = "Option B — performance & scale"
PLAN_CHOICE_GENERATE = "Generate the execution pack"
PLAN_CHOICE_SKIP = "Skip for now"
IMPL_CHOICE_START = "Start implementation"
IMPL_CHOICE_SKIP = "Stop here (blueprint only)"

# Order in which `state["stage"]` advances; used to tell whether a gate has been decided.
_STAGES = [
    "discussion", "summary", "answers", "architecture", "arch_choice", "strategy",
    "plan_gate", "plan_bundle", "implement_gate", "implementation", "verification", "report", "done",
]  # fmt: skip

# Kinds whose entries are messages the user sent (rendered as user bubbles).
USER_KINDS = frozenset({"idea", "user_answers", "arch_decision", "planner_decision", "implement_decision"})

# Graph node about to run -> (speaker, message) for the in-progress indicator.
_RUNNING: dict[str, tuple[str, str]] = {
    "summarizer": ("Summarizer", "Synthesizing the discussion into a summary and MVP questions..."),
    "architect": ("Architect", "Turning your answers into two architecture options..."),
    "strategy": ("Strategy", "Choosing the agent execution strategy..."),
    "plan_bundle": (
        "Planner",
        "Generating the blueprint pack: PRD, architecture doc, plan, and subagent definitions...",
    ),
    "implementer": ("Implementer", "Implementation agents are building the project. This can take a while..."),
    "verifier": ("Verifier", "Running the generated project's test suite..."),
    "delivery_report": ("Delivery", "Assembling the delivery report..."),
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


def _with_notes(text: str, notes: str) -> str:
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


def running_from_state(values: Mapping[str, Any], next_nodes: Sequence[str]) -> tuple[str, str] | None:
    """The (speaker, message) of the step that is about to run, if it is a working step."""
    for node in next_nodes:
        if node == "discussion":
            speaker = str(values.get("next_speaker") or "PM")
            return speaker, f"{speaker} is drafting the next panel turn..."
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
        text = _with_notes(f"Architecture choice: Option {choice['option']}", str(choice.get("notes") or ""))
        entries.append(ViewEntry("arch_decision", "You", text))

    strategy = values.get("execution_strategy") or {}
    if strategy.get("mode"):
        entries.append(ViewEntry("strategy", "Strategy", strategy_markdown(strategy)))

    plan_decision = values.get("plan_decision") or {}
    generated = bool(plan_decision.get("generate"))
    if _reached(values, "plan_bundle"):
        label = PLAN_CHOICE_GENERATE if generated else PLAN_CHOICE_SKIP
        entries.append(ViewEntry("planner_decision", "You", _with_notes(label, str(plan_decision.get("notes") or ""))))

    if bundle := str(values.get("project_bundle_summary") or values.get("project_bundle_dir") or "").strip():
        entries.append(ViewEntry("project_bundle", "Planner", bundle))

    implement_decision = values.get("implement_decision") or {}
    if generated and _reached(values, "implementation"):
        label = IMPL_CHOICE_START if implement_decision.get("implement") else IMPL_CHOICE_SKIP
        notes = str(implement_decision.get("notes") or "")
        entries.append(ViewEntry("implement_decision", "You", _with_notes(label, notes)))

    if log := str(values.get("implementation_log") or "").strip():
        workspace = str(values.get("workspace_dir") or "").strip()
        content = f"**Workspace:** `{workspace}`\n\n{log}" if workspace else log
        entries.append(ViewEntry("implementation", "Implementer", content))

    verification = values.get("verification") or {}
    if str(verification.get("report") or "").strip():
        verdict = "passed ✅" if verification.get("passed") else "failed ❌"
        attempts = int(verification.get("attempts") or 0)
        content = (
            f"Verification {verdict} after {attempts} fix attempt(s).\n\n{str(verification['report']).strip()}"
        )
        entries.append(ViewEntry("verification", "Verifier", content))

    if report := str(values.get("delivery_report") or "").strip():
        entries.append(ViewEntry("delivery_report", "Delivery", report))

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
        return [ViewEntry("implement_gate", "Planner", question or "Start the implementation stage now?")]
    return []  # the answers gate is the questions entry itself

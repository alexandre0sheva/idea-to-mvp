"""Headless runs: the pipeline from the command line (`idea-to-mvp run`, `resume`, `sessions`).

The same graph, checkpointer, and session list as the UI (`graph.read_thread`, `SessionRegistry.record`), so a
headless thread can be opened in the UI and the other way round; only the gate decisions come from flags instead
of a form. A gate the flags do not answer stops the run there (the thread keeps waiting at it), and the
implement gate, which spends money on agent sessions, is passed only with an explicit `--implement`.

Progress goes to the `progress` callback (stderr in the CLI); the result is a `RunSummary` whose `exit_code` is
0 delivered and verified, 2 delivered with failures, 3 stopped at a gate, 1 error.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Literal

from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from idea_to_mvp.config import Settings
from idea_to_mvp.graph import ThreadView, read_thread, run_config, update_thread
from idea_to_mvp.implementation.events import EVENT_KINDS
from idea_to_mvp.nodes.gates import AUTOPILOT_NOTE, recommended_option, suggested_answers
from idea_to_mvp.publish import GitHubClient, PublishPlan, publish_plan
from idea_to_mvp.resilience import error_hint
from idea_to_mvp.schemas import ProjectPreferences
from idea_to_mvp.sessions import SessionInfo, SessionRegistry
from idea_to_mvp.state import make_initial_state
from idea_to_mvp.usage import summarize_usage

LOGGER = logging.getLogger(__name__)

EXIT_VERIFIED = 0
EXIT_ERROR = 1
EXIT_FAILED = 2
EXIT_GATE = 3

Progress = Callable[[str], None]

_GATE_NAMES = {
    "answers": "the answers gate",
    "arch_choice": "the architecture gate",
    "plan_gate": "the plan gate",
    "implement_gate": "the implement gate",
    "iterate_gate": "the iterate gate",
}
_NUMBERED = re.compile(r"^\d+[.)]\s")


@dataclass(frozen=True)
class HeadlessOptions:
    """How the gates are answered. `answers` is the text of the answers file; `arch` the option to build;
    `parallel` the implement gate's parallelism (0 = the `IMPLEMENTER_MAX_PARALLEL` setting); `feedback` the
    change request for the next version (used once)."""

    answers: str | None = None
    autopilot: bool = False
    arch: Literal["A", "B"] | None = None
    implement: bool = False
    parallel: int = 0
    feedback: str | None = None


@dataclass(frozen=True)
class GateAnswer:
    """The resume value for a gate, or None with the reason the run stops there."""

    resume: Any
    note: str


@dataclass
class RunSummary:
    thread_id: str
    title: str
    outcome: Literal["verified", "failed", "gate", "ended", "interrupted", "error"]
    exit_code: int
    message: str
    stage: str = ""
    gate: str | None = None
    iteration: int = 1
    verification: Literal["passed", "failed"] | None = None
    workspace: str = ""
    delivery_zip: str = ""
    blueprint_dir: str = ""
    planning_cost_usd: float | None = None
    agent_cost_usd: float = 0.0
    total_tokens: int = 0
    unpriced_calls: int = 0
    cost_estimate: dict[str, Any] = field(default_factory=dict)
    questions: list[dict[str, str]] = field(default_factory=list)
    architecture_options: dict[str, str] = field(default_factory=dict)
    next_command: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ------------------------------------------------------------------ gates


def number_answers(text: str) -> str:
    """An answers file as the numbered lines the answers gate takes: blank lines are dropped, a line that is
    already numbered (`3. ...`) keeps its number, any other line is numbered by its position."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(line if _NUMBERED.match(line) else f"{index}. {line}" for index, line in enumerate(lines, 1))


def decide(payload: Mapping[str, Any], options: HeadlessOptions) -> GateAnswer:
    """What the flags say at the gate `payload` is from (`payload["kind"]`)."""
    kind = str(payload.get("kind") or "")
    if kind == "answers":
        if options.answers and (answers := number_answers(options.answers)):
            return GateAnswer(answers, "answers from --answers")
        if options.autopilot and (suggested := suggested_answers(payload.get("question_items") or [])):
            return GateAnswer(suggested, "Autopilot: the suggested answers.")
        return GateAnswer(
            None, "The panel's questions need answers: pass --answers FILE (one answer per line) or --autopilot."
        )
    if kind == "arch_choice":
        if options.arch:
            return GateAnswer({"option": options.arch, "notes": "--arch"}, f"Option {options.arch} (--arch)")
        if options.autopilot:
            option = recommended_option(payload.get("proposal") or {})
            return GateAnswer({"option": option, "notes": AUTOPILOT_NOTE}, f"Option {option}: {AUTOPILOT_NOTE}")
        return GateAnswer(None, "Pick an architecture: --arch 1 (option A) or --arch 2 (option B), or --autopilot.")
    if kind == "plan_gate":
        if options.autopilot or options.implement:
            return GateAnswer({"generate": True, "notes": "Headless run: generating the pack."}, "generating the pack")
        return GateAnswer(None, "Generate the blueprint pack with --autopilot or --implement.")
    if kind == "implement_gate":
        if options.implement:
            resume = {"implement": True, "notes": "Headless run: --implement.", "parallel": max(0, options.parallel)}
            return GateAnswer(resume, "building (--implement)")
        return GateAnswer(
            None, f"Stopped before any agent session. Estimated cost: {estimate_text(payload.get('cost_estimate') or {})}."
        )
    if kind == "iterate_gate":
        if options.feedback and options.implement:
            return GateAnswer({"iterate": True, "feedback": options.feedback}, "building the next version (--feedback)")
        if options.feedback:
            return GateAnswer(None, "Building another version spends money: add --implement to --feedback.")
        return GateAnswer(None, "")
    return GateAnswer(None, f"Unknown gate `{kind}`.")


def estimate_text(estimate: Mapping[str, Any]) -> str:
    """The implement gate's `CostRange` in one phrase."""
    if estimate.get("basis") == "unpriced" or estimate.get("low") is None or estimate.get("high") is None:
        roles = ", ".join(str(role) for role in estimate.get("unpriced") or []) or "a model"
        return f"unpriced (no price for the {roles} model)"
    basis = "earlier tasks on this model" if estimate.get("basis") == "history" else "catalog guess"
    return f"${float(estimate['low']):.2f}–${float(estimate['high']):.2f} for {int(estimate.get('tasks') or 0)} task(s) ({basis})"


# ---------------------------------------------------------------- summary


def summarize(thread_id: str, view: ThreadView, *, note: str = "", error: str | None = None) -> RunSummary:
    """The outcome of a thread as it stands, with the exit code that says it."""
    values, gate = view.values, view.interrupt
    usage = summarize_usage(values.get("usage") or [])
    kind = str(gate.get("kind")) if gate else None
    delivered = bool(values.get("delivery_report")) and (kind == "iterate_gate" or (gate is None and not view.next_nodes))
    passed = bool((values.get("verification") or {}).get("passed"))
    summary = RunSummary(
        thread_id=thread_id,
        title=str(values.get("user_idea") or ""),
        outcome="error",
        exit_code=EXIT_ERROR,
        message=note,
        stage=str(values.get("stage") or ""),
        gate=kind,
        iteration=int(values.get("iteration") or 1),
        verification=("passed" if passed else "failed") if delivered else None,
        workspace=str(values.get("workspace_dir") or ""),
        delivery_zip=str(values.get("delivery_zip") or ""),
        blueprint_dir=str(values.get("project_bundle_dir") or ""),
        planning_cost_usd=usage["planning_cost_usd"],
        agent_cost_usd=float(usage["agent_cost_usd"]),
        total_tokens=int(usage["total_tokens"]),
        unpriced_calls=int(usage["unpriced_calls"]),
        cost_estimate=dict((gate or {}).get("cost_estimate") or values.get("cost_estimate") or {}),
    )
    if error is not None:
        summary.message = error
        summary.next_command = f"idea-to-mvp resume {thread_id}"
    elif delivered:
        summary.outcome, summary.exit_code = ("verified", EXIT_VERIFIED) if passed else ("failed", EXIT_FAILED)
        summary.message = note or f"v0.{summary.iteration} delivered; verification {'passed' if passed else 'did not pass'}."
        if kind == "iterate_gate":
            summary.next_command = f'idea-to-mvp resume {thread_id} --implement --feedback "<changes>"'
    elif gate is not None:
        summary.outcome, summary.exit_code = "gate", EXIT_GATE
        summary.message = f"Waiting at {_GATE_NAMES.get(kind or '', kind)}. {note}".strip()
        summary.next_command = f"idea-to-mvp resume {thread_id} {_resume_hint(kind or '')}".strip()
        if kind == "answers":
            summary.questions = [
                {key: str(item.get(key) or "") for key in ("question", "why_it_matters", "suggested_answer")}
                for item in gate.get("question_items") or []
            ]
        if kind == "arch_choice":
            proposal = gate.get("proposal") or {}
            summary.architecture_options = {
                key: str((proposal.get(f"option_{key.lower()}") or {}).get("name") or "") for key in ("A", "B")
            }
            summary.architecture_options["recommended"] = recommended_option(proposal)
    elif view.next_nodes:
        summary.outcome, summary.exit_code = "interrupted", EXIT_ERROR
        summary.message = note or f"The run stopped before {', '.join(view.next_nodes)} finished."
        summary.next_command = f"idea-to-mvp resume {thread_id}"
    elif values.get("user_idea"):
        summary.outcome, summary.exit_code = "ended", EXIT_GATE
        summary.message = note or "The run ended at a gate before anything was built."
    else:
        summary.message = note or f"No session `{thread_id}`: see `idea-to-mvp sessions`."
    return summary


def _resume_hint(kind: str) -> str:
    return {
        "answers": "--answers FILE",
        "arch_choice": "--arch 1",
        "plan_gate": "--autopilot",
        "implement_gate": "--implement",
    }.get(kind, "")


def render_summary(summary: RunSummary) -> str:
    """The summary as the lines the CLI prints to stdout."""
    outcome = {
        "verified": "delivered and verified",
        "failed": "delivered, verification failed",
        "gate": "stopped at a gate",
        "ended": "ended before implementation",
        "interrupted": "interrupted",
        "error": "error",
    }[summary.outcome]
    planning = "unpriced" if summary.planning_cost_usd is None else f"${summary.planning_cost_usd:.2f}"
    unpriced = f" ({summary.unpriced_calls} call(s) unpriced)" if summary.unpriced_calls else ""
    rows = [
        ("Session", summary.thread_id),
        ("Idea", summary.title),
        ("Outcome", outcome if not summary.gate or summary.outcome != "gate" else f"{outcome}: {summary.gate}"),
        ("", summary.message),
        ("Workspace", summary.workspace),
        ("Delivery", summary.delivery_zip),
        ("Blueprint", summary.blueprint_dir),
        ("Cost", f"planning {planning}{unpriced}, agents ${summary.agent_cost_usd:.2f}, {summary.total_tokens:,} tokens"),
        ("Estimate", estimate_text(summary.cost_estimate) if summary.cost_estimate else ""),
    ]
    lines = [f"{label + ':' if label else '':<11}{value}" for label, value in rows if value]
    for number, item in enumerate(summary.questions, 1):
        lines.append(f"  {number}. {item['question']}")
        if item["suggested_answer"]:
            lines.append(f"     suggested: {item['suggested_answer']}")
    if options := summary.architecture_options:
        recommended = options.get("recommended")
        for key, number in (("A", 1), ("B", 2)):
            mark = " (recommended)" if key == recommended else ""
            lines.append(f"  --arch {number}: option {key}, {options.get(key, '')}{mark}")
    if summary.next_command:
        lines.append(f"{'Next:':<11}{summary.next_command}")
    return "\n".join(lines)


# ---------------------------------------------------------------- driving


def progress_lines(namespace: Sequence[str], mode: str, data: Any) -> list[str]:
    """What one streamed item says on the progress channel: committed steps and agent sessions starting and
    ending (tool calls and text are left to the trajectories)."""
    if mode == "updates" and isinstance(data, dict):
        prefix = "".join(f"{part.split(':', 1)[0]}/" for part in namespace)
        return [f"done {prefix}{node}" for node in data if not str(node).startswith("__")]
    if mode == "custom" and isinstance(data, dict) and data.get("kind") in EVENT_KINDS:
        who = " ".join(str(part) for part in (data.get("task_id"), data.get("label")) if part)
        if data["kind"] == "task_start":
            return [f"start {who}"]
        if data["kind"] == "task_end":
            return [f"end   {who}: {data.get('detail') or 'done'}"]
    return []


def _next_step(view: ThreadView, options: HeadlessOptions) -> tuple[Any, HeadlessOptions, str | None]:
    """(payload for the next `astream`, the options after it, None) or (None, options, the reason to stop)."""
    if view.interrupt is None:
        if view.next_nodes:
            return None, options, None  # interrupted mid-step: continue from the last checkpoint
        return None, options, ""  # the run has ended
    answer = decide(view.interrupt, options)
    if answer.resume is None:
        return None, options, answer.note
    if view.interrupt.get("kind") == "iterate_gate":
        options = replace(options, feedback=None)  # one change request, one version
    # autopilot stays off in state: the flags decide every gate here, so `--arch` wins over a recommendation
    return Command(resume=answer.resume, update={"autopilot": False}), options, None


async def drive(
    graph: CompiledStateGraph,
    registry: SessionRegistry,
    thread_id: str,
    payload: Any,
    options: HeadlessOptions,
    *,
    settings: Settings,
    progress: Progress,
    title: str | None = None,
) -> RunSummary:
    """Stream the thread from `payload` (a new state, a `Command`, or None to continue), answering every gate
    the flags answer, until it ends or waits at a gate they do not."""
    config = run_config(thread_id, max_concurrency=settings.llm_max_concurrency)
    started = time.monotonic()
    while True:
        try:
            async for namespace, mode, data in graph.astream(
                payload, config=config, stream_mode=["updates", "custom"], subgraphs=True
            ):
                for line in progress_lines(namespace, mode, data):
                    progress(f"[{time.monotonic() - started:6.0f}s] {line}")
        except Exception as exc:
            view = await read_thread(graph, thread_id)
            registry.record(thread_id, view.values, view.interrupt, title=title)
            return summarize(thread_id, view, error=error_hint("Pipeline", exc))
        view = await read_thread(graph, thread_id)
        registry.record(thread_id, view.values, view.interrupt, title=title)
        payload, options, stop = _next_step(view, options)
        if stop is not None:
            return summarize(thread_id, view, note=stop)
        if view.interrupt is not None:
            progress(f"[{time.monotonic() - started:6.0f}s] gate  {view.interrupt.get('kind')}: {_echo(payload)}")


def _echo(command: Any) -> str:
    resume = getattr(command, "resume", None)
    if isinstance(resume, str):
        return f"{len(resume.splitlines())} answer(s)"
    if isinstance(resume, dict):
        return ", ".join(f"{key}={value}" for key, value in resume.items() if key != "notes" and value != "")
    return ""


async def run_idea(
    graph: CompiledStateGraph,
    registry: SessionRegistry,
    idea: str,
    options: HeadlessOptions,
    *,
    settings: Settings,
    progress: Progress,
    thread_id: str | None = None,
    preferences: ProjectPreferences | None = None,
) -> RunSummary:
    """A new session for `idea` (with the project `preferences` the user stated up front), run as far as the flags
    allow."""
    thread_id = thread_id or str(uuid.uuid4())
    state = make_initial_state(idea, settings.default_rounds, panel_mode=settings.panel_mode, preferences=preferences)
    registry.record(thread_id, {}, None, title=idea)
    progress(f"session {thread_id}")
    return await drive(graph, registry, thread_id, state, options, settings=settings, progress=progress, title=idea)


async def resume_thread(
    graph: CompiledStateGraph,
    registry: SessionRegistry,
    thread_id: str,
    options: HeadlessOptions,
    *,
    settings: Settings,
    progress: Progress,
) -> RunSummary:
    """Carry a saved session on from where it is: the gate it waits at, or the step it was stopped in."""
    view = await read_thread(graph, thread_id)
    if not view.values.get("user_idea"):
        return summarize(thread_id, view)
    payload, options, stop = _next_step(view, options)
    if stop is not None:
        return summarize(thread_id, view, note=stop)
    if view.interrupt is None and view.values.get("autopilot"):
        # a UI run with autopilot on: the flags decide the gates from here
        await graph.aupdate_state({"configurable": {"thread_id": thread_id}}, {"autopilot": False})
    return await drive(graph, registry, thread_id, payload, options, settings=settings, progress=progress)


async def publish_and_record(
    graph: CompiledStateGraph, thread_id: str, plan: PublishPlan, client: GitHubClient, *, confirm: str
) -> tuple[dict[str, Any], str]:
    """Publish a confirmed plan and note it in the thread's state (the UI and `idea-to-mvp publish` share this).

    Returns the record and what went wrong while noting it ('' if nothing): the push has happened either way, so a
    failure to record is reported, not raised. `PublishError` (nothing, or only an empty repository, was made) is raised.
    """
    record = await asyncio.to_thread(publish_plan, plan, client, confirm=confirm)
    try:
        await update_thread(graph, thread_id, {"publication": record})
    except Exception as error:
        LOGGER.warning("Published %s but could not record it in session %s: %s", record["repo"], thread_id, error)
        return record, str(error)
    return record, ""


def session_rows(sessions: Sequence[SessionInfo]) -> list[dict[str, str]]:
    """The saved sessions as rows for `idea-to-mvp sessions`, newest first."""
    return [
        {
            "thread_id": info.thread_id,
            "title": info.title,
            "stage": info.stage,
            "waiting_at": info.interrupted_at or "",
            "updated_at": info.updated_at.isoformat(timespec="seconds"),
        }
        for info in sessions
    ]


def render_sessions(rows: Sequence[Mapping[str, str]]) -> str:
    if not rows:
        return "No saved sessions."
    lines = []
    for row in rows:
        where = f"waiting: {row['waiting_at']}" if row["waiting_at"] else row["stage"]
        lines.append(f"{row['thread_id']}  {row['updated_at'][:16].replace('T', ' ')}  {where:<24}  {row['title']}")
    return "\n".join(lines)

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

from langchain_core.messages import AIMessage, BaseMessage

from idea_to_mvp.implementation.events import ImplEvent
from idea_to_mvp.nodes.common import display_speaker_name, preference_lines, stated_preferences
from idea_to_mvp.roles import SPEAKER_ORDER, TOKEN_TO_SPEAKER
from idea_to_mvp.text_utils import normalize_content, normalize_message_content
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
    {"idea", "preferences", "user_answers", "arch_decision", "planner_decision", "implement_decision", "change_request"}
)

# Graph node about to run -> (speaker, message) for the in-progress indicator.
_RUNNING: dict[str, tuple[str, str]] = {
    "research": ("Researcher", "Searching the web for competitors and market context..."),
    "summarizer": ("Summarizer", "Synthesizing the discussion into a summary and MVP questions..."),
    "architect": ("Architect", "Turning your answers into two architecture options..."),
    "strategy": ("Strategy", "Choosing the agent execution strategy..."),
    "plan_bundle": (
        "Planner",
        "Generating the blueprint pack: PRD, architecture doc, plan, subagent definitions, and the agent harness...",
    ),
    "prepare_workspace": ("Workspace", "Preparing the project workspace (copying the pack, initialising git)..."),
    "acceptance_author": ("Acceptance author", "Writing held-out acceptance tests from the PRD and the contracts..."),
    "harness_tuning": (
        "Harness tuner",
        "Tuning the agent harness: a pilot task is built under the pack's harness and two proposed edits of it...",
    ),
    "implementer": ("Implementer", "Implementation agents are building the project. This can take a while..."),
    "verify": ("Verifier", "Verifying in parallel lanes: tests, quality, requirement coverage, and acceptance tests when there are any..."),
    "fix": ("Fixer", "Fixing what verification found..."),
    "delivery_report": ("Delivery", "Assembling the delivery report..."),
    "reflect": ("Reflector", "Recording what this delivery showed in the experience library..."),
    "change_planner": ("Change planner", "Planning your changes as new tasks..."),
}


@dataclass(frozen=True)
class ViewEntry:
    kind: str
    speaker: str
    content: str
    data: Any = None  # typed payload for richer rendering (e.g. question items)


@dataclass(frozen=True)
class TurnInfo:
    """How a panel turn is shown (the `data` of a discussion entry): the opening statements form one row, turns
    older than the last round are collapsed, and a turn that is still being written is live."""

    phase: str  # "opening" | "turn"
    collapsed: bool = False
    live: bool = False


# Panel subgraph nodes that write a turn, and the phase of the turn they write.
_PANEL_PHASES = {"opening_turn": "opening", "speaker_turn": "turn"}


def panel_token(item: object) -> tuple[str, str, str, str] | None:
    """`(call id, speaker, phase, text)` of a `messages`-mode stream item, if it is a piece of a panel turn.

    The stream carries every model call's tokens; only the two panel turn nodes are shown live. The speaker
    comes from the `role` the runtime tags its model with (`llm.runtime`), so the three parallel openings can
    be told apart.
    """
    if not (isinstance(item, tuple) and len(item) == 2):
        return None
    chunk, metadata = item
    if not isinstance(chunk, BaseMessage) or not isinstance(metadata, Mapping):
        return None
    phase = _PANEL_PHASES.get(str(metadata.get("langgraph_node")))
    speaker = TOKEN_TO_SPEAKER.get(str(metadata.get("role")))
    text = normalize_content(chunk.content)
    if phase is None or speaker is None or not text:
        return None
    return str(chunk.id), speaker, phase, text


class LiveTurns:
    """The panel turns being written right now: the tokens of each speaker's model call, accumulated.

    Transient by nature (the checkpoint only holds a turn once it is finished); the service clears it when the
    graph commits the turns. A retried call (new call id for the same speaker) starts its bubble over. A
    finished turn stays until then, because parallel openings only reach the transcript together.
    """

    def __init__(self) -> None:
        self._turns: dict[str, tuple[str, str, str, bool]] = {}  # speaker -> (call id, phase, text so far, done)

    def add(self, call_id: str, speaker: str, phase: str, text: str) -> None:
        previous_id, _phase, so_far, _done = self._turns.get(speaker, (call_id, phase, "", False))
        self._turns[speaker] = (call_id, phase, (so_far if previous_id == call_id else "") + text, False)

    def finish(self, speaker: str, text: str, *, phase: str = "opening") -> None:
        """The speaker's node returned: its final text replaces the tokens (and is all there is for a model that
        does not stream)."""
        call_id, known_phase, _so_far, _done = self._turns.get(speaker, ("", phase, "", False))
        self._turns[speaker] = (call_id, known_phase, text, True)

    def clear(self) -> None:
        self._turns.clear()

    def __bool__(self) -> bool:
        return bool(self._turns)

    def entries(self) -> list[ViewEntry]:
        order = {name: index for index, name in enumerate(SPEAKER_ORDER)}
        return [
            ViewEntry("discussion", speaker, text, data=TurnInfo(phase, live=not done))
            for speaker, (_id, phase, text, done) in sorted(self._turns.items(), key=lambda kv: order.get(kv[0], 99))
        ]


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


def research_markdown(brief: Mapping[str, Any]) -> str:
    """The research brief as Markdown (for the session export; the chat shows `render.research_card`)."""
    rows: list[str] = []
    if competitors := brief.get("competitors") or []:
        rows += ["**Competitors**", ""]
        for c in competitors:
            rows.append(f"- [{c['name']}]({c['url']}) — {c['positioning']}" + (f" (pricing: {c['pricing']})" if c.get("pricing") else ""))
        rows.append("")
    for title, key in (("Market notes", "market_notes"), ("Gaps", "gaps"), ("Sources", "sources")):
        if items := brief.get(key) or []:
            rows += [f"**{title}**", "", *(f"- {item}" for item in items), ""]
    return "\n".join(rows).strip()


def with_notes(text: str, notes: str) -> str:
    return f"{text} — {notes}" if notes else text


def strategy_markdown(strategy: Mapping[str, Any]) -> str:
    lines = [f"**Execution mode:** `{strategy.get('mode') or 'unknown'}`", ""]
    if (template := str(strategy.get("template") or "")) and template != "none":
        lines += [f"**Starter template:** `{template}` (laid down before the first task)", ""]
    lines += [str(strategy.get("reasoning") or "").strip(), "", "**Workstreams:**"]
    for workstream in strategy.get("workstreams") or []:
        lines.append(f"- `{workstream.get('name') or 'workstream'}` — {workstream.get('focus') or ''}")
    return "\n".join(lines).strip()


ADOPTED_HARNESS = {
    "keep_base": "kept the pack's harness",
    "adopt_inv": "adopted the INV edit (how the agent works)",
    "adopt_dom": "adopted the DOM edit (what a practitioner of the stack knows)",
    "merge": "merged the INV and DOM edits",
}


def _run_line(probe: Mapping[str, Any]) -> str:
    tests = probe.get("tests") or {}
    verdict = "tests not run" if not tests.get("ran") else "tests passed" if tests.get("passed") else "tests failed"
    delivered = "delivered" if probe.get("delivered") else "delivered nothing"
    return f"{delivered}, {verdict} · ${float(probe.get('cost_usd') or 0):.2f}"


ARM_HARNESS = {"base": "the pack's harness", "inv": "the INV edit", "dom": "the DOM edit"}


def selection_sentences(record: Mapping[str, Any]) -> list[str]:
    """What became of the pilot builds ([] before a selection): the build that is the pilot task's result and why,
    or why none is."""
    pilot = str((record.get("pilot") or {}).get("id") or "the pilot task")
    selection = record.get("selection") or {}
    status = selection.get("status")
    rationale = str(selection.get("rationale") or "").strip()
    overruled = str(selection.get("overruled") or "").strip()
    if status == "skipped":
        return [f"No pilot build was selected: {selection.get('note') or 'no reason recorded'}."]
    if status == "none":
        return [f"No pilot build was selected ({overruled or rationale or 'no reason recorded'}); {pilot} is built again."]
    if status != "chosen":
        return []
    build = selection.get("choice")
    origin = ARM_HARNESS.get(str(selection.get("arm")), "an unknown harness")
    if selection.get("landed") is False:
        return [f"Build {build} (built under {origin}) was selected, but {selection.get('note') or 'it was not applied'}."]
    count = len(selection.get("builds") or {})
    sentences = [f"Build {build} of {count} (built under {origin}) was selected as {pilot}'s result, so it is not built again."]
    if rationale:
        sentences.append(rationale)
    if overruled:
        sentences.append(f"The selector chose build {selection.get('proposed')}, overruled: {overruled}.")
    return sentences


def tuning_spend(record: Mapping[str, Any]) -> tuple[float, float]:
    """(the pilot builds and their tests lanes, the model calls) in dollars, from the probes and the ledger."""
    probes = [record.get("base") or {}, *((record.get(kind) or {}).get("probe") or {} for kind in ("inv", "dom"))]
    sessions = sum(float(probe.get("cost_usd") or 0.0) for probe in probes)
    total = float((record.get("ledger") or {}).get("cost_usd") or 0.0)
    return sessions, max(0.0, total - sessions)


def tuning_markdown(record: Mapping[str, Any]) -> str:
    """The chat's short account of harness tuning ('' before it ran): the outcome, the three runs, the composer."""
    status = record.get("status")
    if status == "skipped":
        reason = str(record.get("note") or "").removeprefix("Skipped:").strip().rstrip(".")
        return f"**Harness tuning** skipped: {reason or 'no reason recorded'}."
    if status != "done":
        return ""
    pilot = record.get("pilot") or {}
    compose = record.get("compose") or {}
    lines = [
        f"**Harness tuning** on pilot task `{pilot.get('id', '')}` ({pilot.get('title', '')}): "
        f"{ADOPTED_HARNESS.get(str(record.get('adopted')), str(record.get('adopted')))}.",
        "",  # a list must not run into the paragraph above it
    ]
    lines.append(f"- Base harness: {_run_line(record.get('base') or {})}")
    for kind in ("inv", "dom"):
        candidate = record.get(kind) or {}
        summary = str(candidate.get("summary") or "").strip() or "no edit"
        if candidate.get("inherited"):
            summary = "no usable edit (the base harness ran)"
        lines.append(f"- {kind.upper()} edit: {summary} — {_run_line(candidate.get('probe') or {})}")
    if rationale := str(compose.get("rationale") or "").strip():
        lines.append(f"\n{rationale}")
    if overruled := str(compose.get("overruled") or "").strip():
        lines.append(f"\nThe composer chose `{compose.get('proposed')}`, overruled: {overruled}.")
    ledger = record.get("ledger") or {}
    if ledger and not ledger.get("ok"):
        lines.append("\nThe ledger did not add up, so the base harness was kept: " + "; ".join(ledger.get("problems") or []) + ".")
    if record.get("commit"):
        lines.append(f"\nCommitted as `harness: tuned (pilot {pilot.get('id', '')})` ({record['commit']}).")
    if selected := selection_sentences(record):
        lines.append("\n" + " ".join(selected))
    sessions, calls = tuning_spend(record)
    lines.append(f"\nSpend: ${sessions + calls:.2f} (pilot builds and their tests lanes ${sessions:.2f}, model calls ${calls:.2f}).")
    return "\n".join(lines)


def library_markdown(library: Mapping[str, Any]) -> str:
    """The chat's note that the planner was given an earlier project's lessons ('' when it was given none)."""
    lessons = list(library.get("lessons") or [])
    if not lessons:
        return ""
    return (
        f"**Experience library** (`{library.get('domain', '')}`): the plan writer and the harness writer (and harness "
        f"tuning's editors, when it runs) were given {len(lessons)} lesson(s) from earlier projects of this stack, as "
        "reference data to re-verify."
    )


def reflection_markdown(record: Mapping[str, Any]) -> str:
    """The chat's account of what the delivery's reflection recorded ('' before one ran)."""
    status = record.get("status")
    if not status:
        return ""
    domain = f"`{record.get('domain')}`" if record.get("domain") else "the"
    if status in ("skipped", "failed", "refused"):
        return f"**Experience library** ({domain}): nothing recorded. {record.get('note') or ''}".strip()
    lines = []
    lesson = record.get("lesson") or {}
    if lesson:
        lines.append(
            f"**Experience library** ({domain}): recorded a lesson for the next project of this stack "
            f"(confidence {lesson.get('confidence', '?')}).\n\n"
            f"- When: {lesson.get('cue', '')}\n- Lesson: {lesson.get('lesson', '')}\n- Evidence: {lesson.get('evidence', '')}"
        )
    else:
        lines.append(f"**Experience library** ({domain}): no lesson from this delivery (NO_ENTRY).")
    if edits := record.get("edits") or []:
        lines.append("\nWhat the run tried, judged from its evidence:\n")
        lines += [f"- `{row.get('edit', '')}`: {row.get('effect', '')} — {row.get('evidence', '')}" for row in edits]
    if dropped := record.get("dropped") or []:
        lines.append("\nNot recorded: " + "; ".join(str(item) for item in dropped) + ".")
    if record.get("entry"):
        lines.append(f"\nEntry `{record['entry']}` (browse or forget it in the *Experience library* panel).")
    return "\n".join(lines)


def panel_running(values: Mapping[str, Any]) -> tuple[str, str]:
    """The in-progress indicator for the panel: parallel openings first, then one speaker at a time."""
    if values.get("panel_mode", "moderated") == "moderated" and not values.get("turn_count"):
        return "Panel", "PM, Tech Lead, and Skeptic are writing their opening statements in parallel..."
    speaker = str(values.get("next_speaker") or "PM")
    return speaker, f"{speaker} is drafting the next panel turn..."


_CONSOLE_ACTIONS = 8
_VERIFY_PREFIX = "verify:"  # task ids of the verification lanes and the fix session (implementation/verify.py)
_PILOT_PREFIX = "pilot:"  # task ids of harness tuning's pilot runs and their tests lanes (nodes/harness_tuning.py)


def _by_title(task_id: str) -> bool:
    """Whether a working unit is named by its title alone (a lane, a fix, a pilot run), not as a plan task."""
    return task_id.startswith((_VERIFY_PREFIX, _PILOT_PREFIX))


def _who(task_id: str, label: str) -> str:
    """How a working unit is named: a plan task by id and title, a verification lane or a pilot run by its title."""
    return label if _by_title(task_id) else f"{task_id} {label}"


def _verb(task_ids: Sequence[str]) -> str:
    if task_ids and all(task_id.startswith(_PILOT_PREFIX) for task_id in task_ids):
        return "Tuning the harness"
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
        named = last_label if _by_title(last_task) else last_task
        return f"{_verb([last_task])}: {named} finished · {money}", "\n".join(finished[-_CONSOLE_ACTIONS:])
    names = [(tid, start["label"], start["detail"]) for tid, start in running.items()]
    verb = _verb([tid for tid, _, _ in names])
    if len(names) == 1:
        tid, label, position = names[0]
        head = f"{verb} {tid}" if not _by_title(tid) else verb
        status = f"{head}{f' ({position})' if position else ''}: {label}"
    else:
        noun = {"Verifying": "lanes", "Tuning the harness": "runs"}.get(verb, "tasks")
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
    live: Sequence[ViewEntry] = (),
    error: str | None = None,
) -> list[ViewEntry]:
    """Everything the chat shows, in pipeline order.

    `pending_interrupt` is the payload of the gate the graph is paused at (adds that gate's card).
    `pending_user` / `live` / `running` / `error` are transient overlays for the moment right after a submit:
    the user's message before the graph has recorded it, the panel turns still being written (they replace the
    in-progress indicator), the step in progress, and a failure notice.
    """
    entries: list[ViewEntry] = []
    idea = str(values.get("user_idea") or "").strip()
    if idea:
        entries.append(ViewEntry("idea", "You", idea))
        if lines := preference_lines(stated_preferences(values.get("preferences"))):
            entries.append(ViewEntry("preferences", "You", "Project preferences:\n" + "\n".join(lines)))
        research = values.get("research") or {}
        if research.get("competitors") or research.get("market_notes") or research.get("gaps"):
            entries.append(ViewEntry("research", "Research", research_markdown(research), data=research))

    turns = [m for m in values.get("discussion_history") or [] if isinstance(m, AIMessage)]
    moderated = values.get("panel_mode", "moderated") == "moderated"
    for index, message in enumerate(turns):
        speaker = display_speaker_name(message.name) if message.name else "Panelist"
        info = TurnInfo(
            phase="opening" if moderated and index < len(SPEAKER_ORDER) else "turn",
            collapsed=index < len(turns) - len(SPEAKER_ORDER),  # older than the last round
        )
        entries.append(ViewEntry("discussion", speaker, normalize_message_content(message), data=info))

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
        entries.append(ViewEntry("project_bundle", "Planner", bundle, data=list(values.get("project_bundle_files") or [])))
        if library := library_markdown(values.get("library") or {}):
            entries.append(ViewEntry("library", "Experience library", library))

    implement_decision = values.get("implement_decision") or {}
    if generated and _reached(values, "implementation"):
        label = IMPL_CHOICE_START if implement_decision.get("implement") else IMPL_CHOICE_SKIP
        notes = str(implement_decision.get("notes") or "")
        entries.append(ViewEntry("implement_decision", "You", with_notes(label, notes)))

    if tuning := tuning_markdown(values.get("harness_tuning") or {}):
        entries.append(ViewEntry("harness_tuning", "Harness tuner", tuning, data=values.get("harness_tuning")))

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
        entries.append(ViewEntry("verification", "Verifier", content, data=verification))

    if report := str(values.get("delivery_report") or "").strip():
        entries.append(ViewEntry("delivery_report", "Delivery", report, data=values))  # rendered as the dashboard
        if reflection := reflection_markdown(values.get("reflection") or {}):
            entries.append(ViewEntry("reflection", "Reflector", reflection))

    if no_change := values.get("no_change") or {}:  # the request was not built, so it is not a version above
        entries.append(ViewEntry("change_request", "You", f"Change request: {no_change.get('request', '')}"))
        entries.append(
            ViewEntry("change_planner", "Change planner", f"No change needed: {no_change.get('reason', '')}")
        )

    entries.extend(_gate_card(pending_interrupt) if pending_user is None else [])

    if pending_user is not None:
        entries.append(pending_user)
    entries.extend(live)
    if running is not None and not live:
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

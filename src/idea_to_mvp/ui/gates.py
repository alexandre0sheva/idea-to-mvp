"""The forms of the human-in-the-loop gates: one `GateSpec` per gate, no widgets shared between gates.

A spec says how its gate looks (`build` creates the widgets, `render` fills them from the interrupt payload),
what it sends back (`build_resume`: form inputs -> the value the graph node parses), how to refuse a bad
submission (`validate`), how the decision reads in the chat until the graph records it (`echo`), and which
graph node runs next (`next_step`). The service dispatches through `GATES`; adding a gate is one node plus
one entry here.

The implement gate also carries the blueprint editor: PRD, architecture, and plan can be edited before any
money is spent, and edits are checked on save (`save_blueprint_file`).
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import gradio as gr

from idea_to_mvp.plan import (
    Plan,
    PlanParseError,
    load_plan,
    parse_plan_markdown,
    render_plan_markdown,
    validate_plan,
    workstream_names,
)
from idea_to_mvp.schemas import QUESTION_COUNT
from idea_to_mvp.ui.view import (
    IMPL_CHOICE_SKIP,
    IMPL_CHOICE_START,
    MODE_ANSWERS,
    MODE_ARCH_CHOICE,
    MODE_IMPL_GATE,
    MODE_ITERATE_GATE,
    MODE_PLAN_GATE,
    PLAN_CHOICE_GENERATE,
    PLAN_CHOICE_SKIP,
    ViewEntry,
    with_notes,
)

GateInputs = dict[str, Any]  # field name -> value, plus "action": the name of the button that was pressed

NO_ANSWER = "(no answer)"
BLUEPRINT_FILES = ("PRD.md", "ARCHITECTURE.md", "plan.md")
SEQUENTIAL = "Sequential"


@dataclass(frozen=True)
class GateAction:
    name: str
    label: str
    variant: str = "secondary"


@dataclass(frozen=True)
class GateView:
    """Widget updates for a gate's payload: field name -> `gr.update` keyword arguments."""

    updates: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class GateRuntime:
    """What a gate's own events (fill suggestions, save a file) need from the app."""

    thread: gr.State
    payload: Callable[[str], Awaitable[dict[str, Any]]]  # the interrupt payload the thread is paused at


def _no_validation(inputs: GateInputs, payload: dict[str, Any]) -> str | None:
    return None


def _no_wiring(widgets: dict[str, Any], runtime: GateRuntime) -> None:
    return None


@dataclass(frozen=True)
class GateSpec:
    kind: str
    mode: str  # the UI mode while this gate is pending
    fields: tuple[str, ...]  # data widgets: form inputs and update targets, in layout order
    actions: tuple[GateAction, ...]  # submit buttons, created by `build` as widgets named "action:<name>"
    build: Callable[[], dict[str, Any]]
    render: Callable[[dict[str, Any]], GateView]
    build_resume: Callable[[GateInputs], Any]
    echo: Callable[[GateInputs], ViewEntry]
    next_step: Callable[[Any], str | None]
    validate: Callable[[GateInputs, dict[str, Any]], str | None] = field(default=_no_validation)
    wire: Callable[[dict[str, Any], GateRuntime], None] = field(default=_no_wiring)


def _text(inputs: GateInputs, name: str) -> str:
    return str(inputs.get(name) or "").strip()


def _e(value: Any) -> str:
    return html.escape(str(value or ""))


# ------------------------------------------------------------------- answers

_ANSWER_FIELDS = tuple(f"answer_{n}" for n in range(1, QUESTION_COUNT + 1))
_LEADING_NUMBER = re.compile(r"^\s*(?:\d+[.)]|[-*])\s*")


def _question_texts(payload: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """(question, why it matters, suggested answer) of each question the gate asks."""
    items = [item for item in payload.get("question_items") or [] if isinstance(item, Mapping)]
    if items:
        return [
            (str(i.get("question") or ""), str(i.get("why_it_matters") or ""), str(i.get("suggested_answer") or ""))
            for i in items
        ][:QUESTION_COUNT]
    return [(_LEADING_NUMBER.sub("", str(q)), "", "") for q in payload.get("questions") or []][:QUESTION_COUNT]


def _answers_view(payload: dict[str, Any]) -> GateView:
    questions = _question_texts(payload)
    updates: dict[str, dict[str, Any]] = {}
    for number, name in enumerate(_ANSWER_FIELDS, start=1):
        if number <= len(questions):
            question, why, suggestion = questions[number - 1]
            updates[name] = {"visible": True, "label": f"{number}. {question}", "info": why or None, "value": suggestion}
        else:
            updates[name] = {"visible": False, "label": f"{number}.", "info": None, "value": ""}
    return GateView(updates)


def suggestion_updates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The value of every answer field set back to the question's suggestion (the 'Use all suggestions' button)."""
    return [{"value": update["value"]} for update in _answers_view(payload).updates.values()]


def _answers_resume(inputs: GateInputs) -> str:
    """The numbered lines a typed answer used to be: blank answers keep their number, trailing blanks go."""
    answers = [_text(inputs, name) for name in _ANSWER_FIELDS]
    while answers and not answers[-1]:
        answers.pop()
    return "\n".join(f"{number}. {answer or NO_ANSWER}" for number, answer in enumerate(answers, start=1))


def _answers_validate(inputs: GateInputs, payload: dict[str, Any]) -> str | None:
    if not any(_text(inputs, name) for name in _ANSWER_FIELDS):
        return "Please answer the questions before submitting."
    return None


def _answers_build() -> dict[str, Any]:
    widgets: dict[str, Any] = {}
    gr.Markdown("### Your answers")
    for name in _ANSWER_FIELDS:
        widgets[name] = gr.Textbox(label=name, lines=2, visible=False)
    widgets["use_suggestions"] = gr.Button("Use all suggestions")
    widgets["action:submit"] = gr.Button("Submit answers", variant="primary")
    return widgets


def _answers_wire(widgets: dict[str, Any], runtime: GateRuntime) -> None:
    async def fill(thread_id: str) -> list[Any]:
        return [gr.update(**update) for update in suggestion_updates(await runtime.payload(thread_id))]

    widgets["use_suggestions"].click(fill, inputs=[runtime.thread], outputs=[widgets[name] for name in _ANSWER_FIELDS])


# -------------------------------------------------------------- architecture


def _chips(items: Sequence[Any]) -> str:
    return "".join(f"<span class='chip'>{_e(item)}</span>" for item in items)


def _arch_card(option: Mapping[str, Any], key: str, recommended: bool) -> str:
    badge = "<span class='badge-recommended'>Recommended</span>" if recommended else ""
    tradeoffs = "".join(f"<li>{_e(item)}</li>" for item in option.get("tradeoffs") or [])
    return (
        f"<section class='arch-card{' recommended' if recommended else ''}'>"
        f"<h4>Option {key} — {_e(option.get('name'))} {badge}</h4>"
        f"<p class='arch-style'>{_e(option.get('style'))}</p>"
        f"<div class='chips'>{_chips(option.get('stack') or [])}</div>"
        f"<p><strong>Data:</strong> {_e(option.get('persistence'))}</p>"
        f"<p><strong>Trade-offs</strong></p><ul>{tradeoffs}</ul>"
        f"<p><strong>Limits:</strong> {_e(option.get('limits'))}</p>"
        "</section>"
    )


def _arch_view(payload: dict[str, Any]) -> GateView:
    proposal = payload.get("proposal") or {}
    recommendation = "B" if str(proposal.get("recommendation") or "").strip().upper() == "B" else "A"
    option_a, option_b = proposal.get("option_a") or {}, proposal.get("option_b") or {}
    cards = (
        "<div class='arch-grid'>"
        + _arch_card(option_a, "A", recommendation == "A")
        + _arch_card(option_b, "B", recommendation == "B")
        + "</div>"
        f"<p class='arch-why'><strong>Why option {recommendation}:</strong> {_e(proposal.get('recommendation_rationale'))}</p>"
        f"<p class='arch-why'><strong>Biggest trade-off:</strong> {_e(proposal.get('biggest_tradeoff'))}</p>"
    )
    return GateView(
        {
            "cards": {"value": cards},
            "option": {
                "choices": [(f"Option A — {option_a.get('name') or 'A'}", "A"), (f"Option B — {option_b.get('name') or 'B'}", "B")],
                "value": recommendation,
            },
            "notes": {"value": ""},
        }
    )


def _arch_resume(inputs: GateInputs) -> dict[str, str]:
    option = "B" if str(inputs.get("option") or "").strip().upper() == "B" else "A"
    return {"option": option, "notes": _text(inputs, "notes")}


def _arch_build() -> dict[str, Any]:
    widgets: dict[str, Any] = {}
    gr.Markdown("### Choose the architecture")
    widgets["cards"] = gr.HTML()
    widgets["option"] = gr.Radio(choices=[("Option A", "A"), ("Option B", "B")], value="A", label="Option")
    widgets["notes"] = gr.Textbox(label="Notes on the architecture choice (optional)", lines=2)
    widgets["action:submit"] = gr.Button("Send choice", variant="primary")
    return widgets


# ---------------------------------------------------------------------- plan


def _plan_resume(inputs: GateInputs) -> dict[str, Any]:
    return {"generate": inputs.get("action") == "generate", "notes": _text(inputs, "notes")}


def _plan_build() -> dict[str, Any]:
    widgets: dict[str, Any] = {}
    gr.Markdown("### Execution pack")
    widgets["notes"] = gr.Textbox(label="Planning notes (optional)", placeholder="Anything the planner should account for...", lines=2)
    with gr.Row():
        widgets["action:generate"] = gr.Button("Generate the execution pack", variant="primary")
        widgets["action:skip"] = gr.Button("Skip for now")
    return widgets


# ----------------------------------------------------------------- blueprint


@dataclass(frozen=True)
class SaveResult:
    ok: bool
    issues: list[str]
    text: str = ""  # what the file now contains (plan.md is rewritten canonically)


def read_blueprint_file(bundle_dir: str | Path, name: str) -> str:
    """The text of an editable blueprint document ('' for anything else, or when it cannot be read)."""
    if name not in BLUEPRINT_FILES:
        return ""
    try:
        return (Path(bundle_dir) / name).read_text(encoding="utf-8")
    except OSError:
        return ""


def _workstreams(bundle: Path) -> list[str]:
    try:
        strategy = json.loads((bundle / "STRATEGY.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        strategy = None
    return workstream_names(strategy if isinstance(strategy, dict) else None)


def _plan_issues(plan: Plan, prd: str, workstreams: list[str]) -> list[str]:
    return validate_plan(plan, prd_markdown=prd, workstreams=workstreams)


def save_blueprint_file(bundle_dir: str | Path, name: str, text: str) -> SaveResult:
    """Validate and write an edited blueprint document.

    Anything is refused when empty. A `PRD.md` edit is checked against the current plan (requirements the
    plan cites must still exist); a `plan.md` edit is read back into a plan and checked (cycles, unknown
    ids, ...), then `plan.json` (the source of truth) and a canonical `plan.md` are written. Only problems the
    edit *introduces* block it: a pack that already had review notes can still be improved. Nothing is
    written when there are issues.
    """
    bundle = Path(bundle_dir)
    if name not in BLUEPRINT_FILES:
        return SaveResult(False, [f"{name} is not an editable document"])
    if not text.strip():
        return SaveResult(False, [f"{name} would be empty"])
    workstreams = _workstreams(bundle)
    prd = read_blueprint_file(bundle, "PRD.md")
    current = load_plan(_read_json(bundle))
    baseline = set(_plan_issues(current, prd, workstreams)) if current else set()

    if name == "plan.md":
        try:
            plan = parse_plan_markdown(text)
        except PlanParseError as error:
            return SaveResult(False, error.issues)
        issues = [i for i in _plan_issues(plan, prd, workstreams) if i not in baseline]
        if issues:
            return SaveResult(False, issues)
        canonical = render_plan_markdown(plan)
        (bundle / "plan.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
        (bundle / "plan.md").write_text(canonical, encoding="utf-8")
        return SaveResult(True, [], canonical)

    if name == "PRD.md" and current is not None:
        issues = [i for i in _plan_issues(current, text, workstreams) if i not in baseline]
        if issues:
            return SaveResult(False, [f"the plan no longer fits this PRD: {issue}" for issue in issues])
    (bundle / name).write_text(text, encoding="utf-8")
    return SaveResult(True, [], text)


def _read_json(bundle: Path) -> str:
    try:
        return (bundle / "plan.json").read_text(encoding="utf-8")
    except OSError:
        return ""


def _save_message(name: str, result: SaveResult) -> str:
    if result.ok:
        return f"Saved `{name}`."
    return f"**Not saved.** `{name}` has problems:\n" + "\n".join(f"- {issue}" for issue in result.issues)


# ----------------------------------------------------------------- implement


def _row(label: str, value: Any) -> str:
    return f"<tr><th>{_e(label)}</th><td>{value}</td></tr>"


def _estimate(payload: Mapping[str, Any]) -> str:
    tasks = int(payload.get("task_count") or 0)
    fixes = int(payload.get("max_fix_attempts") or 0)
    build = f"{tasks} task sessions" if payload.get("strategy_mode") == "agent_team" and tasks else "1 lead session"
    return f"≈ {build} + 3 verification lanes, plus up to {fixes} fix round(s)"


def _parallel_choices(payload: Mapping[str, Any]) -> tuple[list[str], str | None, bool]:
    """(choices, default, whether it can be changed): Sequential, or Parallel 2..width of the plan."""
    if payload.get("strategy_mode") != "agent_team":
        return [SEQUENTIAL], None, False  # a lead session builds everything: nothing to parallelise
    width = max(1, int(payload.get("dag_width") or 1))
    choices = [SEQUENTIAL, *(f"Parallel {n}" for n in range(2, width + 1))]
    preferred = min(width, max(1, int(payload.get("max_parallel") or 1)))
    return choices, choices[preferred - 1], width > 1


def _implement_view(payload: dict[str, Any]) -> GateView:
    sandbox = payload.get("sandbox") or {}
    table = (
        "<table class='gate-table'><tbody>"
        + _row("Model", f"<code>{_e(payload.get('model'))}</code>")
        + _row("Sandbox", _e(sandbox.get("summary")))
        + _row("Permission mode", f"<code>{_e(payload.get('permission_mode'))}</code>")
        + _row("Tasks", _e(payload.get("task_count", 0)))
        + _row("DAG width", _e(payload.get("dag_width", 0)))
        + _row("Budget", f"${float(payload.get('max_total_usd') or 0):.2f} total, ${float(payload.get('max_task_usd') or 0):.2f} per task")
        + _row("Estimated sessions", _e(_estimate(payload)))
        + "</tbody></table>"
    )
    warning = str(payload.get("warning") or "").strip()
    review = [str(note) for note in payload.get("review_issues") or [] if str(note).strip()]
    choices, default, interactive = _parallel_choices(payload)
    bundle = str(payload.get("bundle_dir") or "")
    return GateView(
        {
            "banner": {"visible": bool(warning), "value": f"<div class='gate-banner danger'>⚠️ {_e(warning)}</div>" if warning else ""},
            "summary": {"value": table},
            "review": {
                "visible": bool(review),
                "value": "<div class='gate-banner warn'><strong>Open review notes</strong><ul>"
                + "".join(f"<li>{_e(note)}</li>" for note in review)
                + "</ul></div>"
                if review
                else "",
            },
            "parallel": {"choices": choices, "value": default, "interactive": interactive},
            "file": {"choices": list(BLUEPRINT_FILES), "value": BLUEPRINT_FILES[0]},
            "editor": {"value": read_blueprint_file(bundle, BLUEPRINT_FILES[0])},
            "save_status": {"value": ""},
            "notes": {"value": ""},
        }
    )


def _parallel_of(choice: Any) -> int:
    """Tasks at once from the selector: 'Sequential' -> 1, 'Parallel 3' -> 3, nothing chosen -> 0 (the setting)."""
    match = re.search(r"(\d+)\s*$", str(choice or ""))
    if match:
        return max(1, int(match.group(1)))
    return 1 if str(choice or "") == SEQUENTIAL else 0


def _implement_resume(inputs: GateInputs) -> dict[str, Any]:
    return {
        "implement": inputs.get("action") == "start",
        "notes": _text(inputs, "notes"),
        "parallel": _parallel_of(inputs.get("parallel")),
    }


def _implement_validate(inputs: GateInputs, payload: dict[str, Any]) -> str | None:
    if inputs.get("action") != "start":
        return None
    name = str(inputs.get("file") or "")
    on_disk = read_blueprint_file(str(payload.get("bundle_dir") or ""), name)
    edited = str(inputs.get("editor") or "")
    if name in BLUEPRINT_FILES and edited.replace("\r\n", "\n").rstrip() != on_disk.replace("\r\n", "\n").rstrip():
        return f"You have unsaved changes in {name}. Save them (or reload the file) before starting the implementation."
    return None


def _implement_build() -> dict[str, Any]:
    widgets: dict[str, Any] = {}
    gr.Markdown("### Before you spend money")
    widgets["banner"] = gr.HTML(visible=False)
    widgets["summary"] = gr.HTML()
    widgets["review"] = gr.HTML(visible=False)
    widgets["parallel"] = gr.Dropdown(choices=[SEQUENTIAL], value=SEQUENTIAL, label="Parallelism", info="Tasks that can run at the same time, each in its own git worktree.")
    with gr.Accordion("Blueprint: read and edit before building", open=False):
        widgets["file"] = gr.Dropdown(choices=list(BLUEPRINT_FILES), value=BLUEPRINT_FILES[0], label="Document")
        widgets["editor"] = gr.Code(label="Contents", language="markdown", lines=18, interactive=True)
        widgets["save"] = gr.Button("Save changes")
        widgets["save_status"] = gr.Markdown()
    widgets["notes"] = gr.Textbox(label="Notes for the implementation agents (optional)", placeholder="Priorities, stack preferences...", lines=2)
    with gr.Row():
        widgets["action:start"] = gr.Button("Start implementation", variant="primary")
        widgets["action:skip"] = gr.Button("Stop here (blueprint only)")
    return widgets


def _implement_wire(widgets: dict[str, Any], runtime: GateRuntime) -> None:
    async def load(thread_id: str, name: str) -> Any:
        payload = await runtime.payload(thread_id)
        return gr.update(value=read_blueprint_file(str(payload.get("bundle_dir") or ""), name)), gr.update(value="")

    async def save(thread_id: str, name: str, text: str) -> tuple[Any, Any]:
        payload = await runtime.payload(thread_id)
        result = save_blueprint_file(str(payload.get("bundle_dir") or ""), name, text)
        return (gr.update(value=result.text) if result.ok else gr.update()), gr.update(value=_save_message(name, result))

    widgets["file"].change(load, inputs=[runtime.thread, widgets["file"]], outputs=[widgets["editor"], widgets["save_status"]])
    widgets["save"].click(
        save, inputs=[runtime.thread, widgets["file"], widgets["editor"]], outputs=[widgets["editor"], widgets["save_status"]]
    )


# ------------------------------------------------------------------- iterate


def _iterate_view(payload: dict[str, Any]) -> GateView:
    version = int(payload.get("iteration") or 1) + 1
    return GateView({"feedback": {"label": f"What should change in v0.{version}?", "value": ""}})


def _iterate_resume(inputs: GateInputs) -> dict[str, Any]:
    iterate = inputs.get("action") == "iterate"
    return {"iterate": iterate, "feedback": _text(inputs, "feedback") if iterate else ""}


def _iterate_validate(inputs: GateInputs, payload: dict[str, Any]) -> str | None:
    if inputs.get("action") == "iterate" and not _text(inputs, "feedback"):
        return "Describe the changes you want for the next version first."
    return None


def _iterate_build() -> dict[str, Any]:
    widgets: dict[str, Any] = {}
    gr.Markdown("### Next version")
    widgets["feedback"] = gr.Textbox(label="What should change in the next version?", placeholder="Add ... / fix ... / change ...", lines=3)
    with gr.Row():
        widgets["action:iterate"] = gr.Button("Iterate", variant="primary")
        widgets["action:finish"] = gr.Button("Finish")
    return widgets


# ------------------------------------------------------------------ registry


def _entry(kind: str, speaker: str, content: str) -> ViewEntry:
    return ViewEntry(kind, speaker, content)


GATES: dict[str, GateSpec] = {
    "answers": GateSpec(
        kind="answers",
        mode=MODE_ANSWERS,
        fields=_ANSWER_FIELDS,
        actions=(GateAction("submit", "Submit answers", "primary"),),
        build=_answers_build,
        render=_answers_view,
        build_resume=_answers_resume,
        echo=lambda inputs: _entry("user_answers", "You", _answers_resume(inputs)),
        next_step=lambda resume: "architect",
        validate=_answers_validate,
        wire=_answers_wire,
    ),
    "arch_choice": GateSpec(
        kind="arch_choice",
        mode=MODE_ARCH_CHOICE,
        fields=("cards", "option", "notes"),
        actions=(GateAction("submit", "Send choice", "primary"),),
        build=_arch_build,
        render=_arch_view,
        build_resume=_arch_resume,
        echo=lambda inputs: _entry(
            "arch_decision", "You", with_notes(f"Architecture choice: Option {_arch_resume(inputs)['option']}", _text(inputs, "notes"))
        ),
        next_step=lambda resume: "strategy",
    ),
    "plan_gate": GateSpec(
        kind="plan_gate",
        mode=MODE_PLAN_GATE,
        fields=("notes",),
        actions=(GateAction("generate", "Generate the execution pack", "primary"), GateAction("skip", "Skip for now")),
        build=_plan_build,
        render=lambda payload: GateView({"notes": {"value": ""}}),
        build_resume=_plan_resume,
        echo=lambda inputs: _entry(
            "planner_decision",
            "You",
            with_notes(PLAN_CHOICE_GENERATE if inputs.get("action") == "generate" else PLAN_CHOICE_SKIP, _text(inputs, "notes")),
        ),
        next_step=lambda resume: "plan_bundle" if resume.get("generate") else None,
    ),
    "implement_gate": GateSpec(
        kind="implement_gate",
        mode=MODE_IMPL_GATE,
        fields=("banner", "summary", "review", "parallel", "file", "editor", "save_status", "notes"),
        actions=(GateAction("start", "Start implementation", "primary"), GateAction("skip", "Stop here (blueprint only)")),
        build=_implement_build,
        render=_implement_view,
        build_resume=_implement_resume,
        echo=lambda inputs: _entry(
            "implement_decision",
            "You",
            with_notes(IMPL_CHOICE_START if inputs.get("action") == "start" else IMPL_CHOICE_SKIP, _text(inputs, "notes")),
        ),
        next_step=lambda resume: "implementer" if resume.get("implement") else None,
        validate=_implement_validate,
        wire=_implement_wire,
    ),
    "iterate_gate": GateSpec(
        kind="iterate_gate",
        mode=MODE_ITERATE_GATE,
        fields=("feedback",),
        actions=(GateAction("iterate", "Iterate", "primary"), GateAction("finish", "Finish")),
        build=_iterate_build,
        render=_iterate_view,
        build_resume=_iterate_resume,
        echo=lambda inputs: _entry(
            "change_request",
            "You",
            f"Change request: {_text(inputs, 'feedback')}" if inputs.get("action") == "iterate" else "Finish here",
        ),
        next_step=lambda resume: "change_planner" if resume.get("iterate") else None,
        validate=_iterate_validate,
    ),
}


# -------------------------------------------------------------------- layout


def gate_layout() -> list[tuple[str, str]]:
    """Every data widget of every gate as (gate kind, field name), in the order the app lists them."""
    return [(kind, name) for kind, spec in GATES.items() for name in spec.fields]


def pack_inputs(kind: str, action: str, values: Sequence[Any]) -> GateInputs:
    """One gate's inputs from the flat list of every gate's field values (in `gate_layout` order)."""
    inputs: GateInputs = {"action": action}
    for (owner, name), value in zip(gate_layout(), values, strict=False):
        if owner == kind:
            inputs[name] = value
    return inputs


def gate_outputs(active: tuple[str, dict[str, Any]] | None) -> list[Any]:
    """Updates for the gate panels (shown only for the pending gate) and for every gate widget: the pending
    gate's widgets are filled from its payload, all others are left alone."""
    kind, payload = active if active and active[0] in GATES else (None, {})
    view = GATES[kind].render(payload) if kind else GateView({})
    panels = [gr.update(visible=owner == kind) for owner in GATES]
    fields = [
        gr.update(**view.updates[name]) if owner == kind and name in view.updates else gr.update()
        for owner, name in gate_layout()
    ]
    return [*panels, *fields]

"""Gate forms: what each gate shows, and that what it sends is what its graph node expects."""

import json
from pathlib import Path
from typing import Any

import gradio as gr
import pytest
from plan_helpers import PRD, WORKSTREAMS, make_plan, make_task

from idea_to_mvp.config import clear_settings_cache
from idea_to_mvp.demo import fixtures
from idea_to_mvp.implementation import options as opts
from idea_to_mvp.nodes import gates as gate_nodes
from idea_to_mvp.nodes import iterate as iterate_node
from idea_to_mvp.plan import load_plan, render_plan_markdown
from idea_to_mvp.state import make_initial_state
from idea_to_mvp.ui.gates import (
    BLUEPRINT_FILES,
    GATES,
    gate_layout,
    gate_outputs,
    pack_inputs,
    read_blueprint_file,
    save_blueprint_file,
    suggestion_updates,
)

ITEMS = [
    {"question": f"Question {i}?", "why_it_matters": f"Reason {i}", "suggested_answer": f"Suggestion {i}"} for i in range(1, 6)
]


@pytest.fixture(autouse=True)
def sandbox_available(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)  # Settings reads .env from the working directory: never the developer's own
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setattr(opts, "sandbox_supported", lambda *a, **k: True)
    clear_settings_cache()
    yield
    clear_settings_cache()


def capture(monkeypatch: pytest.MonkeyPatch, module: Any, node: Any, state: dict[str, Any], reply: Any = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run a gate node with `interrupt` faked: (the payload it pauses with, what the node returns for `reply`)."""
    seen: dict[str, Any] = {}

    def fake_interrupt(payload: dict[str, Any]) -> Any:
        seen.update(payload)
        return reply

    monkeypatch.setattr(module, "interrupt", fake_interrupt)
    update = node(state)
    return seen, update


def base_state(**fields: Any) -> Any:
    state: dict[str, Any] = dict(make_initial_state("A climbing app", 1))
    state.update(fields)
    return state


def resume_through(monkeypatch: pytest.MonkeyPatch, module: Any, node: Any, state: dict[str, Any], resume: Any) -> dict[str, Any]:
    """What the graph node writes into the state when the form's resume value comes back."""
    return capture(monkeypatch, module, node, state, resume)[1]


# ------------------------------------------------------------------ registry


def test_every_gate_of_the_graph_has_a_spec() -> None:
    assert list(GATES) == ["answers", "arch_choice", "plan_gate", "implement_gate", "iterate_gate"]
    assert all(spec.kind == kind for kind, spec in GATES.items())


def test_no_gate_shares_a_widget_with_another() -> None:
    components: list[Any] = []
    with gr.Blocks():
        for spec in GATES.values():
            widgets = spec.build()
            assert set(spec.fields) <= set(widgets)
            assert {f"action:{action.name}" for action in spec.actions} <= set(widgets)
            components += list(widgets.values())
    assert len({id(component) for component in components}) == len(components)


def test_the_layout_lists_every_field_once_in_gate_order() -> None:
    layout = gate_layout()
    assert layout == [(kind, name) for kind, spec in GATES.items() for name in spec.fields]


def test_inputs_are_packed_per_gate_from_the_flat_values() -> None:
    layout = gate_layout()
    values = [f"v-{kind}-{name}" for kind, name in layout]
    packed = pack_inputs("plan_gate", "generate", values)
    assert packed == {"action": "generate", "notes": "v-plan_gate-notes"}
    assert pack_inputs("answers", "submit", values)["answer_3"] == "v-answers-answer_3"


def test_without_a_pending_gate_every_form_is_hidden_and_untouched() -> None:
    outputs = gate_outputs(None)
    panels = outputs[: len(GATES)]
    assert all(update["visible"] is False for update in panels)
    assert len(outputs) == len(GATES) + len(gate_layout())


def test_only_the_pending_gate_is_shown_and_only_its_widgets_are_updated() -> None:
    outputs = gate_outputs(("plan_gate", {"kind": "plan_gate", "question": "Generate?"}))
    assert [update.get("visible") for update in outputs[: len(GATES)]] == [False, False, True, False, False]
    for (kind, _name), update in zip(gate_layout(), outputs[len(GATES) :], strict=True):
        changed = set(update) - {"__type__"}
        assert bool(changed) == (kind == "plan_gate"), (kind, _name)


# ------------------------------------------------------------------- answers


def answers_payload(monkeypatch: pytest.MonkeyPatch, items: list[dict] | None = ITEMS) -> dict[str, Any]:
    state = base_state(
        summary="Brief",
        questions=items or [],
        generated_questions=[f"{i}. Question {i}?" for i in range(1, 6)],
    )
    return capture(monkeypatch, gate_nodes, gate_nodes.collect_answers_node, state, "x")[0]


def test_the_answers_form_has_five_labelled_fields_prefilled_with_the_suggestions(monkeypatch: pytest.MonkeyPatch) -> None:
    view = GATES["answers"].render(answers_payload(monkeypatch))
    for i in range(1, 6):
        field = view.updates[f"answer_{i}"]
        assert field["value"] == f"Suggestion {i}" and f"Question {i}?" in field["label"] and field["info"] == f"Reason {i}"
        assert field["visible"] is True


def test_using_all_suggestions_refills_every_field(monkeypatch: pytest.MonkeyPatch) -> None:
    updates = suggestion_updates(answers_payload(monkeypatch))
    assert [update["value"] for update in updates] == [f"Suggestion {i}" for i in range(1, 6)]


def test_the_suggested_answers_become_the_five_numbered_lines_the_graph_expects(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = answers_payload(monkeypatch)
    inputs = {"action": "submit", **{name: update["value"] for name, update in GATES["answers"].render(payload).updates.items()}}
    resume = GATES["answers"].build_resume(inputs)
    assert resume == "\n".join(f"{i}. Suggestion {i}" for i in range(1, 6))
    written = resume_through(monkeypatch, gate_nodes, gate_nodes.collect_answers_node, base_state(questions=ITEMS), resume)
    assert written["user_answers"] == resume and written["stage"] == "answers"


def test_edited_and_blank_answers_keep_their_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    inputs = {"action": "submit", "answer_1": "Climbers", "answer_2": "  ", "answer_3": "Web", "answer_4": "", "answer_5": "Free"}
    lines = GATES["answers"].build_resume(inputs).splitlines()
    assert lines == ["1. Climbers", "2. (no answer)", "3. Web", "4. (no answer)", "5. Free"]


def test_only_the_questions_that_exist_get_a_field(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {**answers_payload(monkeypatch), "question_items": ITEMS[:2], "questions": ["1. Question 1?", "2. Question 2?"]}
    view = GATES["answers"].render(payload)
    assert [view.updates[f"answer_{i}"]["visible"] for i in range(1, 6)] == [True, True, False, False, False]
    assert view.updates["answer_3"]["value"] == ""
    inputs = {"action": "submit", "answer_1": "a", "answer_2": "b", "answer_3": "", "answer_4": "", "answer_5": ""}
    assert GATES["answers"].build_resume(inputs) == "1. a\n2. b"  # trailing blanks are dropped


def test_questions_without_typed_details_still_get_fields_but_no_suggestions(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"kind": "answers", "questions": ["1. Who is it for?", "2. What is v0?"], "question_items": []}
    view = GATES["answers"].render(payload)
    assert view.updates["answer_1"]["label"].endswith("Who is it for?") and view.updates["answer_1"]["value"] == ""
    assert view.updates["answer_3"]["visible"] is False


def test_submitting_nothing_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = GATES["answers"]
    blank = {"action": "submit", **{f"answer_{i}": "" for i in range(1, 6)}}
    assert "answer the questions" in (spec.validate(blank, {}) or "").lower()
    assert spec.validate({**blank, "answer_2": "something"}, {}) is None


# -------------------------------------------------------------- architecture


def arch_payload(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> dict[str, Any]:
    proposal = fixtures.architecture_proposal([]).model_dump()
    proposal.update(overrides)
    return capture(monkeypatch, gate_nodes, gate_nodes.arch_choice_node, base_state(architecture_proposal=proposal), {"option": "A"})[0]


def test_the_architecture_gate_shows_two_cards_with_the_recommendation_marked(monkeypatch: pytest.MonkeyPatch) -> None:
    view = GATES["arch_choice"].render(arch_payload(monkeypatch))
    html = view.updates["cards"]["value"]
    assert html.count("class='arch-card") == 2
    for text in ("Modular monolith", "API + SPA with workers", "FastAPI", "Redis", "Single-node ceiling", "More moving parts", "Hundreds of concurrent users"):
        assert text in html
    assert html.count("Recommended") == 1
    card_a = html.split("class='arch-card")[1]
    assert "Recommended" in card_a and "Modular monolith" in card_a  # A is the recommendation in the demo fixture
    assert "It ships fastest" in html  # the rationale
    assert view.updates["option"]["value"] == "A"
    assert [choice[1] for choice in view.updates["option"]["choices"]] == ["A", "B"]


def test_the_default_choice_follows_the_recommendation(monkeypatch: pytest.MonkeyPatch) -> None:
    view = GATES["arch_choice"].render(arch_payload(monkeypatch, recommendation="B"))
    assert view.updates["option"]["value"] == "B"
    html = view.updates["cards"]["value"]
    assert "Recommended" in html.split("class='arch-card")[2]


def test_architecture_text_is_escaped(monkeypatch: pytest.MonkeyPatch) -> None:
    proposal = fixtures.architecture_proposal([]).model_dump()
    proposal["option_a"]["name"] = "<script>alert(1)</script>"
    payload = capture(monkeypatch, gate_nodes, gate_nodes.arch_choice_node, base_state(architecture_proposal=proposal), {"option": "A"})[0]
    assert "<script>" not in GATES["arch_choice"].render(payload).updates["cards"]["value"]


def test_the_architecture_choice_round_trips_through_the_node(monkeypatch: pytest.MonkeyPatch) -> None:
    for option in ("A", "B"):
        resume = GATES["arch_choice"].build_resume({"action": "submit", "option": option, "notes": " keep it simple "})
        written = resume_through(monkeypatch, gate_nodes, gate_nodes.arch_choice_node, base_state(), resume)
        assert written["arch_choice"] == {"option": option, "notes": "keep it simple"}


def test_a_missing_architecture_option_falls_back_to_a() -> None:
    assert GATES["arch_choice"].build_resume({"action": "submit", "option": None, "notes": ""})["option"] == "A"


# ---------------------------------------------------------------------- plan


def test_the_plan_gate_generates_or_skips_by_the_button_pressed(monkeypatch: pytest.MonkeyPatch) -> None:
    for action, generate in (("generate", True), ("skip", False)):
        resume = GATES["plan_gate"].build_resume({"action": action, "notes": "lean"})
        written = resume_through(monkeypatch, gate_nodes, gate_nodes.plan_gate_node, base_state(), resume)
        assert written["plan_decision"] == {"generate": generate, "notes": "lean"}
    assert [a.name for a in GATES["plan_gate"].actions] == ["generate", "skip"]


# ----------------------------------------------------------------- implement


@pytest.fixture()
def bundle(tmp_path: Path) -> Path:
    path = tmp_path / "blueprints" / "20260101-000000-000000-idea"
    path.mkdir(parents=True)
    plan = make_plan(
        [
            make_task("T01", requirement_ids=["R1", "R2"], contracts_out=["C1"]),
            make_task("T02", workstream="web-ui", depends_on=["T01"], contracts_in=["C1"]),
            make_task("T03", workstream="web-ui", depends_on=["T01"]),
        ]
    )
    (path / "PRD.md").write_text(PRD)
    (path / "ARCHITECTURE.md").write_text("# Architecture\nA monolith.\n")
    (path / "plan.json").write_text(plan.model_dump_json(indent=2))
    (path / "plan.md").write_text(render_plan_markdown(plan))
    (path / "STRATEGY.json").write_text(json.dumps({"mode": "agent_team", "workstreams": [{"name": n} for n in WORKSTREAMS]}))
    return path


def implement_payload(monkeypatch: pytest.MonkeyPatch, bundle: Path, env: dict[str, str] | None = None, **state: Any) -> dict[str, Any]:
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    clear_settings_cache()
    fields = {"project_bundle_dir": str(bundle), "execution_strategy": {"mode": "agent_team", "workstreams": []}, **state}
    return capture(monkeypatch, gate_nodes, gate_nodes.implement_gate_node, base_state(**fields), {"implement": False})[0]


def test_the_implement_gate_summarises_what_the_run_will_do_and_cost(monkeypatch: pytest.MonkeyPatch, bundle: Path) -> None:
    payload = implement_payload(monkeypatch, bundle, {"IMPLEMENTER_SANDBOX": "on", "IMPLEMENTER_MODEL": "claude-test-1"})
    html = GATES["implement_gate"].render(payload).updates["summary"]["value"]
    for text in ("claude-test-1", "acceptEdits", "on (", "$25.00", "$5.00", "Tasks", ">3<", "DAG width", ">2<"):
        assert text in html, text
    assert "Estimated sessions" in html and "3 task sessions" in html and "3 verification" in html


def test_a_risky_setup_gets_a_red_banner_and_a_safe_one_does_not(monkeypatch: pytest.MonkeyPatch, bundle: Path) -> None:
    risky = implement_payload(monkeypatch, bundle, {"IMPLEMENTER_SANDBOX": "off"})
    banner = GATES["implement_gate"].render(risky).updates["banner"]
    assert banner["visible"] is True and "gate-banner danger" in banner["value"] and "sandbox" in banner["value"].lower()
    safe = implement_payload(monkeypatch, bundle, {"IMPLEMENTER_SANDBOX": "on", "IMPLEMENTER_PERMISSION_MODE": "acceptEdits"})
    assert GATES["implement_gate"].render(safe).updates["banner"]["visible"] is False


def test_open_review_notes_are_shown(monkeypatch: pytest.MonkeyPatch, bundle: Path) -> None:
    payload = {**implement_payload(monkeypatch, bundle), "review_issues": ["T02 has vague acceptance criteria."]}
    review = GATES["implement_gate"].render(payload).updates["review"]
    assert review["visible"] is True and "T02 has vague acceptance criteria." in review["value"]
    clean = GATES["implement_gate"].render({**payload, "review_issues": []}).updates["review"]
    assert clean["visible"] is False


def test_the_parallelism_selector_offers_sequential_and_up_to_the_plans_width(monkeypatch: pytest.MonkeyPatch, bundle: Path) -> None:
    payload = implement_payload(monkeypatch, bundle, {"IMPLEMENTER_MAX_PARALLEL": "2"})
    selector = GATES["implement_gate"].render(payload).updates["parallel"]
    assert list(selector["choices"]) == ["Sequential", "Parallel 2"]  # the plan's width is 2
    assert selector["value"] == "Parallel 2" and selector["interactive"] is True
    wide = GATES["implement_gate"].render({**payload, "dag_width": 4, "max_parallel": 3}).updates["parallel"]
    assert wide["choices"] == ["Sequential", "Parallel 2", "Parallel 3", "Parallel 4"] and wide["value"] == "Parallel 3"


def test_a_narrow_plan_or_a_lead_session_has_nothing_to_parallelise(monkeypatch: pytest.MonkeyPatch, bundle: Path) -> None:
    payload = implement_payload(monkeypatch, bundle)
    narrow = GATES["implement_gate"].render({**payload, "dag_width": 1}).updates["parallel"]
    assert narrow["choices"] == ["Sequential"] and narrow["value"] == "Sequential"
    lead = GATES["implement_gate"].render({**payload, "strategy_mode": "subagents"}).updates["parallel"]
    assert lead["interactive"] is False


def test_starting_carries_the_notes_and_the_chosen_parallelism_into_the_node(monkeypatch: pytest.MonkeyPatch, bundle: Path) -> None:
    spec = GATES["implement_gate"]
    for choice, expected in (("Sequential", 1), ("Parallel 3", 3), (None, 0)):
        resume = spec.build_resume({"action": "start", "notes": "go", "parallel": choice})
        written = resume_through(monkeypatch, gate_nodes, gate_nodes.implement_gate_node, base_state(project_bundle_dir=str(bundle)), resume)
        assert written["implement_decision"] == {"implement": True, "notes": "go", "parallel": expected}
    stop = spec.build_resume({"action": "skip", "notes": "", "parallel": "Parallel 3"})
    assert resume_through(monkeypatch, gate_nodes, gate_nodes.implement_gate_node, base_state(project_bundle_dir=str(bundle)), stop)["implement_decision"]["implement"] is False


def test_unsaved_blueprint_edits_block_the_start(bundle: Path) -> None:
    spec = GATES["implement_gate"]
    payload = {"bundle_dir": str(bundle)}
    on_disk = (bundle / "PRD.md").read_text()
    assert spec.validate({"action": "start", "file": "PRD.md", "editor": on_disk}, payload) is None
    message = spec.validate({"action": "start", "file": "PRD.md", "editor": on_disk + "\n- R9 (P0): more"}, payload) or ""
    assert "unsaved" in message.lower() and "PRD.md" in message
    assert spec.validate({"action": "skip", "file": "PRD.md", "editor": "anything"}, payload) is None  # stopping loses nothing


# ----------------------------------------------------------------- blueprint


def test_the_editor_offers_the_documents_that_matter_before_spending_money() -> None:
    assert BLUEPRINT_FILES == ("PRD.md", "ARCHITECTURE.md", "plan.md")


def test_reading_a_blueprint_file_gives_its_text_and_nothing_for_an_unknown_name(bundle: Path) -> None:
    assert read_blueprint_file(bundle, "PRD.md") == PRD
    assert read_blueprint_file(bundle, "../secrets.txt") == "" and read_blueprint_file(bundle, "nope.md") == ""


def test_a_valid_prd_edit_is_saved(bundle: Path) -> None:
    edited = PRD + "- R4 (P1): share a climb\n"
    result = save_blueprint_file(bundle, "PRD.md", edited)
    assert result.ok and result.issues == [] and (bundle / "PRD.md").read_text() == edited


def test_an_architecture_edit_is_saved_and_an_empty_file_is_not(bundle: Path) -> None:
    assert save_blueprint_file(bundle, "ARCHITECTURE.md", "# New\n").ok
    result = save_blueprint_file(bundle, "ARCHITECTURE.md", "   \n")
    assert not result.ok and "empty" in result.issues[0] and (bundle / "ARCHITECTURE.md").read_text() == "# New\n"


def test_a_plan_edit_updates_plan_json_and_rewrites_plan_md_canonically(bundle: Path) -> None:
    text = (bundle / "plan.md").read_text().replace("### T03. Task T03", "### T03. Renamed task", 1)
    result = save_blueprint_file(bundle, "plan.md", text)
    assert result.ok and result.text == (bundle / "plan.md").read_text()
    plan = load_plan((bundle / "plan.json").read_text())
    assert plan is not None and plan.tasks[2].title == "Renamed task"


def test_a_plan_edited_into_a_cycle_is_rejected_with_the_issue_and_nothing_is_written(bundle: Path) -> None:
    before = {name: (bundle / name).read_text() for name in ("plan.md", "plan.json")}
    text = before["plan.md"].replace("### T01. Task T01\n- Goal: Deliver a slice.\n- Workstream: backend-api.\n- Depends on: none.", "### T01. Task T01\n- Goal: Deliver a slice.\n- Workstream: backend-api.\n- Depends on: T02.", 1)
    assert text != before["plan.md"]
    result = save_blueprint_file(bundle, "plan.md", text)
    assert not result.ok and any("cycle" in issue for issue in result.issues)
    assert {name: (bundle / name).read_text() for name in before} == before


def test_a_plan_with_an_unknown_dependency_or_an_unreadable_layout_is_rejected(bundle: Path) -> None:
    text = (bundle / "plan.md").read_text()
    unknown = save_blueprint_file(bundle, "plan.md", text.replace("- Depends on: T01.", "- Depends on: T99.", 1))
    assert not unknown.ok and any("T99" in issue for issue in unknown.issues)
    unreadable = save_blueprint_file(bundle, "plan.md", "I rewrote the plan as prose.")
    assert not unreadable.ok and any("no tasks" in issue for issue in unreadable.issues)


def test_a_prd_edit_that_orphans_the_plans_requirements_is_rejected(bundle: Path) -> None:
    result = save_blueprint_file(bundle, "PRD.md", "# PRD\n\n- R1 (P0): only this one\n")
    assert not result.ok and any("R2" in issue for issue in result.issues)
    assert (bundle / "PRD.md").read_text() == PRD


def test_problems_the_plan_already_had_do_not_block_an_unrelated_save(bundle: Path) -> None:
    plan = make_plan([make_task("T01", requirement_ids=["R1"])])  # R2 (P0) is covered by no task, and web-ui has none
    (bundle / "plan.json").write_text(plan.model_dump_json())
    (bundle / "plan.md").write_text(render_plan_markdown(plan))
    assert save_blueprint_file(bundle, "ARCHITECTURE.md", "# Better\n").ok
    assert save_blueprint_file(bundle, "plan.md", (bundle / "plan.md").read_text().replace("Task T01", "Setup", 1)).ok


def test_only_the_editable_documents_can_be_written(bundle: Path) -> None:
    for name in ("../evil.md", "plan.json", "AGENTS.md", "REVIEW.md"):
        result = save_blueprint_file(bundle, name, "x")
        assert not result.ok and not (bundle.parent / "evil.md").exists()
    assert (bundle / "AGENTS.md").exists() is False


# ------------------------------------------------------------------- iterate


def test_the_iterate_form_is_a_feedback_box_with_iterate_and_finish(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = GATES["iterate_gate"]
    assert [a.name for a in spec.actions] == ["iterate", "finish"] and spec.fields == ("feedback",)
    assert "v0.3" in spec.render({"kind": "iterate_gate", "iteration": 2}).updates["feedback"]["label"]
    assert "describe" in (spec.validate({"action": "iterate", "feedback": " "}, {}) or "").lower()
    assert spec.validate({"action": "iterate", "feedback": "Add CSV"}, {}) is None
    assert spec.validate({"action": "finish", "feedback": ""}, {}) is None


def test_iterate_and_finish_round_trip_through_the_node(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = GATES["iterate_gate"]
    state = base_state(iteration=1, delivery_report="r", verification={"passed": True, "attempts": 0, "report": "", "lanes": []})
    go = resume_through(monkeypatch, iterate_node, iterate_node.iterate_gate_node, state, spec.build_resume({"action": "iterate", "feedback": " Add CSV "}))
    assert go["iterate_decision"] == {"iterate": True, "feedback": "Add CSV"} and go["iteration"] == 2
    done = resume_through(monkeypatch, iterate_node, iterate_node.iterate_gate_node, state, spec.build_resume({"action": "finish", "feedback": "ignored"}))
    assert done["iterate_decision"] == {"iterate": False, "feedback": ""}


# ---------------------------------------------------- what the chat shows / runs next


def test_each_decision_is_echoed_as_the_users_chat_message_and_names_the_step_that_runs_next() -> None:
    answers = GATES["answers"]
    inputs = {"action": "submit", **{f"answer_{i}": f"a{i}" for i in range(1, 6)}}
    assert answers.echo(inputs).kind == "user_answers" and answers.echo(inputs).content.startswith("1. a1")
    assert answers.next_step(answers.build_resume(inputs)) == "architect"

    arch = GATES["arch_choice"]
    assert arch.echo({"action": "submit", "option": "B", "notes": "lean"}).content == "Architecture choice: Option B — lean"
    assert arch.next_step({"option": "B", "notes": ""}) == "strategy"

    plan = GATES["plan_gate"]
    assert plan.echo({"action": "generate", "notes": ""}).content == "Generate the execution pack"
    assert plan.echo({"action": "skip", "notes": "later"}).content == "Skip for now — later"
    assert plan.next_step({"generate": True, "notes": ""}) == "plan_bundle" and plan.next_step({"generate": False, "notes": ""}) is None

    implement = GATES["implement_gate"]
    assert implement.echo({"action": "start", "notes": "go"}).content == "Start implementation — go"
    assert implement.echo({"action": "skip", "notes": ""}).content == "Stop here (blueprint only)"
    assert implement.next_step({"implement": True}) == "implementer" and implement.next_step({"implement": False}) is None

    iterate = GATES["iterate_gate"]
    assert iterate.echo({"action": "iterate", "feedback": "Add CSV"}).content == "Change request: Add CSV"
    assert iterate.next_step({"iterate": True, "feedback": "x"}) == "change_planner" and iterate.next_step({"iterate": False, "feedback": ""}) is None


# --------------------------------------------- the forms in a real (demo) session


async def to_gate(service: Any, thread: str, kind: str) -> None:
    from service_helpers import mode_of, submit

    from idea_to_mvp.ui.view import IMPL_CHOICE_START, MODE_IMPL_GATE  # noqa: F401

    await submit(service, "A habit tracker for climbing gyms", "", thread)
    if kind == "answers":
        return
    await submit(service, "1. Solo climbers.", "", thread)
    if kind == "arch_choice":
        return
    await submit(service, "", "", thread)
    if kind == "plan_gate":
        return
    await submit(service, "", "Generate the execution pack", thread)
    assert await mode_of(service, thread) == MODE_IMPL_GATE


async def test_the_answers_form_arrives_prefilled_and_its_suggestions_reach_the_graph(demo_service: Any) -> None:
    from service_helpers import POS_FIELDS, gate_field, submit, update_value

    service, graphs = demo_service
    thread = "gf-answers"
    outputs = await submit(service, "A habit tracker for climbing gyms", "", thread)
    fields = [update_value(gate_field(outputs[-1], "answers", f"answer_{i}"), "value") for i in range(1, 6)]
    assert all(fields) and POS_FIELDS > 0

    inputs = {"action": "submit", **{f"answer_{i}": value for i, value in enumerate(fields, start=1)}}
    await submit(service, "", "", thread, gate_inputs=inputs)
    graph = await graphs.get()
    values = (await graph.aget_state({"configurable": {"thread_id": thread}})).values
    assert values["user_answers"].splitlines() == [f"{i}. {value}" for i, value in enumerate(fields, start=1)]


async def test_the_architecture_form_shows_cards_with_the_recommendation_preselected(demo_service: Any) -> None:
    from service_helpers import gate_field, panel_visible, submit, update_value

    service, _ = demo_service
    thread = "gf-arch"
    await to_gate(service, thread, "arch_choice")
    outputs = await submit(service, "", "", thread, gate_inputs={"action": "submit", "option": "B", "notes": ""})  # any valid choice
    assert panel_visible(outputs[-1], "plan_gate") is True  # the next gate's form replaces it
    assert panel_visible(outputs[-1], "arch_choice") is False
    values, interrupt, _ = await service._read("gf-arch")
    assert values["arch_choice"]["option"] == "B" and interrupt["kind"] == "plan_gate"
    assert update_value(gate_field(outputs[-1], "plan_gate", "notes"), "value") == ""


async def test_the_implement_form_shows_the_summary_and_refuses_unsaved_blueprint_edits(demo_service: Any) -> None:
    from service_helpers import gate_field, mode_of, panel_visible, submit, update_value

    from idea_to_mvp.ui.view import MODE_IMPL_GATE

    service, _ = demo_service
    thread = "gf-impl"
    await to_gate(service, thread, "implement_gate")
    outputs = await submit(service, "", "", thread, gate_inputs={"action": "skip", "notes": "", "file": "PRD.md", "editor": "x"})
    assert outputs and await mode_of(service, thread) != MODE_IMPL_GATE  # stopping never needs the editor to be saved

    thread = "gf-impl-2"
    await to_gate(service, thread, "implement_gate")
    _values, interrupt, _next = await service._read(thread)
    on_disk = (Path(interrupt["bundle_dir"]) / "PRD.md").read_text()
    outputs = await submit(
        service, "", "", thread, gate_inputs={"action": "start", "notes": "", "file": "PRD.md", "editor": on_disk + "\nunsaved"}
    )
    assert len(outputs) == 1 and "unsaved" in str(update_value(outputs[0][0], "value")).lower()
    assert await mode_of(service, thread) == MODE_IMPL_GATE  # nothing ran
    summary = update_value(gate_field(outputs[0], "implement_gate", "summary"), "value")
    assert panel_visible(outputs[0], "implement_gate") is True and "Estimated sessions" in str(summary)


async def test_a_blueprint_edit_saved_at_the_gate_is_what_gets_built(demo_service: Any) -> None:
    from service_helpers import mode_of, submit

    from idea_to_mvp.ui.view import MODE_ITERATE_GATE

    service, _ = demo_service
    thread = "gf-edit"
    await to_gate(service, thread, "implement_gate")
    values, interrupt, _ = await service._read(thread)
    bundle = Path(interrupt["bundle_dir"])
    edited = (bundle / "PRD.md").read_text() + "\n## Note from the owner\nKeep the UI dark.\n"
    result = save_blueprint_file(bundle, "PRD.md", edited)
    assert result.ok

    inputs = {"action": "start", "notes": "", "parallel": None, "file": "PRD.md", "editor": edited}
    await submit(service, "", "", thread, gate_inputs=inputs)
    assert await mode_of(service, thread) == MODE_ITERATE_GATE
    values, _, _ = await service._read(thread)
    assert "Keep the UI dark." in (Path(values["workspace_dir"]) / "PRD.md").read_text()


async def test_a_gate_payload_is_available_to_the_forms_own_events(demo_service: Any) -> None:
    service, _ = demo_service
    await to_gate(service, "gf-payload", "answers")
    payload = await service.gate_payload("gf-payload")
    assert payload["kind"] == "answers" and len(suggestion_updates(payload)) == 5
    assert await service.gate_payload("no-such-thread") == {}


async def test_the_forms_disappear_while_a_decision_is_being_sent_and_the_main_box_returns_after_the_run(demo_service: Any) -> None:
    from service_helpers import POS_BUTTON, POS_INPUT, panel_visible, submit, update_value

    service, _ = demo_service
    thread = "gf-hide"
    await submit(service, "A habit tracker for climbing gyms", "", thread)
    outputs = await submit(service, "", "", thread, gate_inputs={"action": "submit", **{f"answer_{i}": "x" for i in range(1, 6)}})
    assert panel_visible(outputs[0], "answers") is False  # the overlay while the graph runs
    assert update_value(outputs[0][POS_INPUT], "visible") is False and update_value(outputs[0][POS_BUTTON], "visible") is False

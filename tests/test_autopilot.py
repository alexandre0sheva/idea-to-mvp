"""Autopilot: the answers, architecture, and plan gates answer themselves; the implement gate never does."""

from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver
from service_helpers import chat_text, mode_of, submit

from idea_to_mvp.graph import build_graph, run_config
from idea_to_mvp.nodes import gates
from idea_to_mvp.state import make_initial_state
from idea_to_mvp.ui.view import IMPL_CHOICE_START, MODE_ANSWERS, MODE_IMPL_GATE, MODE_ITERATE_GATE

IDEA = "A habit tracker for climbing gyms"
ITEMS = [
    {"question": f"Question {i}?", "why_it_matters": "Scope", "suggested_answer": f"Suggestion {i}"} for i in range(1, 6)
]


@pytest.fixture()
def no_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(payload: Any) -> Any:
        raise AssertionError(f"the gate must not pause on autopilot: {payload.get('kind')}")

    monkeypatch.setattr(gates, "interrupt", boom)


def autopilot_state(**fields: Any) -> Any:
    state: dict[str, Any] = dict(make_initial_state(IDEA, 1, autopilot=True))
    state.update(fields)
    return state


# ------------------------------------------------------------------- state


def test_autopilot_is_off_by_default_and_lives_in_the_state() -> None:
    assert make_initial_state(IDEA, 1)["autopilot"] is False
    assert make_initial_state(IDEA, 1, autopilot=True)["autopilot"] is True


# ------------------------------------------------------------------- nodes


def test_the_answers_gate_answers_with_each_questions_suggestion(no_interrupt: None) -> None:
    update = gates.collect_answers_node(autopilot_state(questions=ITEMS))
    assert update["user_answers"] == "\n".join(f"{i}. Suggestion {i}" for i in range(1, 6))
    assert update["stage"] == "answers"


def test_the_answers_gate_still_asks_when_there_are_no_suggestions_to_use(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []
    monkeypatch.setattr(gates, "interrupt", lambda payload: asked.append(payload["kind"]) or "my answers")
    for items in ([], [{"question": "Q?", "why_it_matters": "x", "suggested_answer": ""}]):
        update = gates.collect_answers_node(autopilot_state(questions=items))
        assert update["user_answers"] == "my answers"
    assert asked == ["answers", "answers"]


def test_the_architecture_gate_picks_the_architects_recommendation(no_interrupt: None) -> None:
    for recommended in ("A", "B"):
        update = gates.arch_choice_node(autopilot_state(architecture_proposal={"recommendation": recommended}))
        assert update["arch_choice"]["option"] == recommended
        assert "utopilot" in update["arch_choice"]["notes"]


def test_the_architecture_gate_falls_back_to_option_a_without_a_recommendation(no_interrupt: None) -> None:
    assert gates.arch_choice_node(autopilot_state(architecture_proposal={}))["arch_choice"]["option"] == "A"
    assert gates.arch_choice_node(autopilot_state(architecture_proposal={"recommendation": "C"}))["arch_choice"]["option"] == "A"


def test_the_plan_gate_generates_the_pack(no_interrupt: None) -> None:
    update = gates.plan_gate_node(autopilot_state())
    assert update["plan_decision"]["generate"] is True and update["stage"] == "plan_bundle"


def test_the_implement_gate_and_the_iterate_gate_always_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    from idea_to_mvp.nodes import iterate

    asked: list[str] = []

    def ask(payload: dict[str, Any]) -> Any:
        asked.append(payload["kind"])
        return {"implement": False, "notes": ""} if payload["kind"] == "implement_gate" else {"iterate": False, "feedback": ""}

    monkeypatch.setattr(gates, "interrupt", ask)
    monkeypatch.setattr(iterate, "interrupt", ask)
    gates.implement_gate_node(autopilot_state())
    iterate.iterate_gate_node(autopilot_state())
    assert asked == ["implement_gate", "iterate_gate"]


def test_without_autopilot_every_gate_pauses(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []
    monkeypatch.setattr(gates, "interrupt", lambda payload: asked.append(payload["kind"]) or "x")
    state = dict(make_initial_state(IDEA, 1), questions=ITEMS)
    gates.collect_answers_node(state)  # type: ignore[arg-type]
    gates.arch_choice_node(state)  # type: ignore[arg-type]
    gates.plan_gate_node(state)  # type: ignore[arg-type]
    assert asked == ["answers", "arch_choice", "plan_gate"]


# ------------------------------------------------------------------- graph


async def test_a_demo_run_on_autopilot_stops_only_at_the_implement_gate(demo_env: Path) -> None:
    graph = build_graph(MemorySaver())
    config = run_config("autopilot-graph")
    await graph.ainvoke(make_initial_state(IDEA, 1, autopilot=True), config)
    snapshot = await graph.aget_state(config)
    (interrupt,) = [i for task in snapshot.tasks for i in task.interrupts]
    assert interrupt.value["kind"] == "implement_gate"
    values = snapshot.values
    assert values["user_answers"].startswith("1. ") and values["arch_choice"]["option"] in ("A", "B")
    assert values["plan_decision"]["generate"] is True and values["project_bundle_files"]
    assert values["implement_decision"]["implement"] is False  # it was not decided for the user


# ----------------------------------------------------------------- service


async def test_the_ui_runs_on_autopilot_up_to_the_implement_gate_and_no_further(demo_service: Any) -> None:
    service, graphs = demo_service
    outputs = await submit(service, IDEA, "", "ap-ui", autopilot=True)
    assert await mode_of(service, "ap-ui") == MODE_IMPL_GATE
    assert "Autopilot" in chat_text(outputs[-1])  # the transcript says these choices were automatic
    graph = await graphs.get()
    assert (await graph.aget_state({"configurable": {"thread_id": "ap-ui"}})).values["autopilot"] is True

    await submit(service, "", IMPL_CHOICE_START, "ap-ui", autopilot=True)
    assert await mode_of(service, "ap-ui") == MODE_ITERATE_GATE  # the iterate gate still asks


async def test_switching_autopilot_on_at_a_gate_takes_over_from_there(demo_service: Any) -> None:
    service, graphs = demo_service
    await submit(service, IDEA, "", "ap-toggle", autopilot=False)
    assert await mode_of(service, "ap-toggle") == MODE_ANSWERS
    await submit(service, "1. Solo climbers.", "", "ap-toggle", autopilot=True)
    assert await mode_of(service, "ap-toggle") == MODE_IMPL_GATE
    graph = await graphs.get()
    assert (await graph.aget_state({"configurable": {"thread_id": "ap-toggle"}})).values["autopilot"] is True


async def test_the_settings_panel_mode_reaches_the_new_run(demo_service: Any) -> None:
    service, graphs = demo_service
    await submit(service, IDEA, "", "ap-panel", panel_mode="round_robin")
    graph = await graphs.get()
    values = (await graph.aget_state({"configurable": {"thread_id": "ap-panel"}})).values
    assert values["panel_mode"] == "round_robin" and values["autopilot"] is False

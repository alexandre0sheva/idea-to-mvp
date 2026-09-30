"""SubmitService behaviour on a real graph (demo models, SQLite checkpointer)."""

import json
from pathlib import Path

import pytest
from service_helpers import (
    POS_BUTTON,
    POS_ROUNDS,
    POS_SESSIONS,
    POS_STATUS,
    POS_THREAD,
    POS_TRACKER,
    chat_text,
    gate_field,
    mode_of,
    panel_visible,
    submit,
    update_value,
)

from idea_to_mvp import graph as graph_module
from idea_to_mvp.ui.view import (
    ARCH_CHOICE_B,
    IMPL_CHOICE_SKIP,
    MODE_ANSWERS,
    MODE_ARCH_CHOICE,
    MODE_DONE,
    MODE_IDEA,
    MODE_IMPL_GATE,
    MODE_INTERRUPTED,
    MODE_PLAN_GATE,
    PLAN_CHOICE_GENERATE,
    PLAN_CHOICE_SKIP,
)


async def _to_arch_gate(service, thread: str) -> None:
    await submit(service, "Build a climbing app", "", thread)
    await submit(service, "1. Solo climbers.", "", thread)
    assert await mode_of(service, thread) == MODE_ARCH_CHOICE


async def _to_plan_gate(service, thread: str) -> None:
    await _to_arch_gate(service, thread)
    await submit(service, "prefer simple", ARCH_CHOICE_B, thread)
    assert await mode_of(service, thread) == MODE_PLAN_GATE


async def test_idea_runs_to_the_answers_gate_and_streams_progress(demo_service) -> None:
    service, _ = demo_service
    outputs = await submit(service, "Build a climbing app", "", "t1")
    assert await mode_of(service, "t1") == MODE_ANSWERS
    first, last = outputs[0], outputs[-1]
    assert "opening statements in parallel" in chat_text(first)  # optimistic overlay before the graph moved
    assert "Build a climbing app" in chat_text(last)
    assert panel_visible(last, "answers") is True and panel_visible(last, "arch_choice") is False  # its own form
    assert update_value(last[POS_BUTTON], "visible") is False  # the main run button is not the gate's button
    assert update_value(gate_field(last, "answers", "answer_1"), "value")  # prefilled with the suggestion
    assert update_value(last[POS_ROUNDS], "visible") is False
    assert "Answer the MVP" in update_value(last[POS_STATUS], "value")
    assert "stage-step active' aria-current='step'><span class='stage-label'>Answers" in update_value(last[POS_TRACKER], "value")


async def test_empty_idea_and_empty_answers_are_rejected_without_running_the_graph(demo_service) -> None:
    service, _ = demo_service
    outputs = await submit(service, "   ", "", "t2")
    assert len(outputs) == 1 and "describe your idea" in update_value(outputs[0][POS_STATUS], "value")
    assert await mode_of(service, "t2") == MODE_IDEA

    await submit(service, "Build x", "", "t2")
    outputs = await submit(service, "", "", "t2")
    assert "answer the questions" in update_value(outputs[0][POS_STATUS], "value").lower()
    assert await mode_of(service, "t2") == MODE_ANSWERS


async def test_gate_decisions_reach_the_graph_as_resume_payloads(demo_service) -> None:
    service, graphs = demo_service
    thread = "t3"
    await _to_plan_gate(service, thread)
    graph = await graphs.get()
    values = (await graph.aget_state({"configurable": {"thread_id": thread}})).values
    assert values["arch_choice"] == {"option": "B", "notes": "prefer simple"}
    assert values["execution_strategy"]["mode"] in ("subagents", "agent_team")

    outputs = await submit(service, "keep it lean", PLAN_CHOICE_GENERATE, thread)
    assert await mode_of(service, thread) == MODE_IMPL_GATE
    values = (await graph.aget_state({"configurable": {"thread_id": thread}})).values
    assert values["plan_decision"] == {"generate": True, "notes": "keep it lean"}
    assert "Generate the execution pack — keep it lean" in chat_text(outputs[-1])
    assert panel_visible(outputs[-1], "implement_gate") is True and panel_visible(outputs[-1], "plan_gate") is False


async def test_declining_the_plan_ends_the_run(demo_service) -> None:
    service, graphs = demo_service
    thread = "t4"
    await _to_plan_gate(service, thread)
    outputs = await submit(service, "", PLAN_CHOICE_SKIP, thread)
    assert await mode_of(service, thread) == MODE_DONE
    assert "Skip for now" in chat_text(outputs[-1])
    assert update_value(outputs[-1][POS_ROUNDS], "visible") is True  # ready for the next idea
    assert "Pipeline finished" in update_value(outputs[-1][POS_STATUS], "value")


async def test_declining_implementation_ends_after_the_blueprint(demo_service) -> None:
    service, graphs = demo_service
    thread = "t5"
    await _to_plan_gate(service, thread)
    await submit(service, "", PLAN_CHOICE_GENERATE, thread)
    outputs = await submit(service, "", IMPL_CHOICE_SKIP, thread)
    assert await mode_of(service, thread) == MODE_DONE
    text = chat_text(outputs[-1])
    assert "Stop here (blueprint only)" in text and "Delivery report" not in text


async def test_new_idea_after_completion_starts_a_fresh_thread(demo_service) -> None:
    service, _ = demo_service
    await _to_plan_gate(service, "old")
    await submit(service, "", PLAN_CHOICE_SKIP, "old")
    outputs = await submit(service, "A second idea", "", "old")
    new_thread = outputs[-1][POS_THREAD]
    assert new_thread != "old"
    assert "A second idea" in chat_text(outputs[-1]) and "climbing" not in chat_text(outputs[-1])
    assert await mode_of(service, "old") == MODE_DONE  # the old session is untouched


async def test_failure_mid_run_shows_the_error_and_can_continue(
    demo_service, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _ = demo_service
    thread = "t6"
    await _to_arch_gate(service, thread)

    monkeypatch.setattr(graph_module, "strategy_node", lambda state: (_ for _ in ()).throw(TimeoutError("slow")))
    service._context.graphs._graph = None  # recompile so the patched node is used
    await service._context.graphs.aclose()
    outputs = await submit(service, "", "", thread)  # arch gate: default option A
    final = outputs[-1]
    assert "error" in chat_text(final).lower() and "timed out" in chat_text(final)
    assert "Run failed" in update_value(final[POS_STATUS], "value")
    assert await mode_of(service, thread) == MODE_INTERRUPTED  # gate decision kept; strategy still to run
    assert update_value(final[POS_BUTTON], "value") == "Continue"

    monkeypatch.undo()
    await service._context.graphs.aclose()
    outputs = await submit(service, "", "", thread)  # Continue
    assert await mode_of(service, thread) == MODE_PLAN_GATE
    assert "error" not in chat_text(outputs[-1]).lower()


async def test_clear_gives_a_new_empty_thread(demo_service) -> None:
    service, _ = demo_service
    output = await service.clear_session()
    assert output[POS_THREAD] and output[1] == []
    assert update_value(output[POS_BUTTON], "value") == "Run discussion"


async def test_sessions_can_be_listed_resumed_and_deleted(demo_service) -> None:
    service, graphs = demo_service
    await submit(service, "Idea about climbing", "", "s1")
    await submit(service, "Idea about cooking", "", "s2")

    listing = await service.initial_view("s2")
    choices = update_value(listing[POS_SESSIONS], "choices")
    assert [value for _label, value in choices] == ["s2", "s1"]
    assert "cooking" in choices[0][0] and "waiting: answers" in choices[0][0]

    resumed = await service.load_session("s1", "s2")
    assert resumed[POS_THREAD] == "s1"
    assert "climbing" in chat_text(resumed) and "cooking" not in chat_text(resumed)
    assert panel_visible(resumed, "answers") is True  # back at its gate, with its own form

    assert "Pick a saved session" in update_value((await service.load_session(None, "s1"))[POS_STATUS], "value")

    after = await service.delete_session("s1", "s2")
    assert after[POS_THREAD] == "s2"
    graph = await graphs.get()
    assert not (await graph.aget_state({"configurable": {"thread_id": "s1"}})).values
    assert [s.thread_id for s in service._context.sessions.list()] == ["s2"]

    cleared = await service.delete_session("s2", "s2")  # deleting the open session resets the view
    assert cleared[POS_THREAD] != "s2" and cleared[1] == []


async def test_save_conversation_exports_the_state_derived_transcript(demo_service, demo_env: Path) -> None:
    service, _ = demo_service
    await submit(service, "Export me please", "", "s3")
    status, file_update = await service.save_conversation("", "s3")
    markdown, json_path = (Path(p) for p in update_value(file_update, "value"))
    assert markdown.parent == json_path.parent == demo_env / "exports"
    assert json.loads(json_path.read_text(encoding="utf-8"))["idea"] == "Export me please"
    text = markdown.read_text(encoding="utf-8")
    assert "Export me please" in text and "## Panel Discussion" in text and "## Summary" in text
    assert "Saved conversation" in update_value(status, "value")

    empty_status, _ = await service.save_conversation("", "never-used")
    assert "Nothing to save" in update_value(empty_status, "value")


async def test_panel_turns_reach_the_chat_while_the_panel_is_still_running(demo_service) -> None:
    service, _ = demo_service
    outputs = await submit(service, "Build a climbing app", "", "t-panel", rounds=3)
    texts = [chat_text(output) for output in outputs]
    # The panel is a subgraph: its turns must show up before the summarizer has run.
    assert any("Pushback on functionality" in t and "Executive summary" not in t for t in texts)
    assert "Moderator" in texts[-1] and "converged" in texts[-1]  # the early stop is explained
    assert await mode_of(service, "t-panel") == MODE_ANSWERS


async def test_panel_text_streams_into_the_chat_token_by_token(demo_service, monkeypatch: pytest.MonkeyPatch) -> None:
    from idea_to_mvp.ui import service as service_module

    monkeypatch.setattr(service_module, "_TOKEN_INTERVAL_SECONDS", 0.0)  # render every token batch
    service, _ = demo_service
    outputs = await submit(service, "Build a climbing app", "", "t-stream", rounds=3)
    texts = [chat_text(output) for output in outputs]
    final = texts[-1]
    # some render shows a turn that is still being written; once the graph committed the turns none is left
    live_renders = [t for t in texts if "live-turn" in t]
    assert live_renders and all("Build a climbing app" in t for t in live_renders)  # a turn in progress, in context
    assert "live-turn" not in final
    assert final.count("class='opening-row'") == 1  # the three openings sit in one row
    assert "moderator-banner" in final and "converged" in final


async def test_preferences_given_with_the_idea_become_part_of_the_run(demo_service) -> None:
    service, _ = demo_service
    prefs = {"platform": "web", "stack_hints": "TypeScript + Postgres", "deploy_target": "Fly.io"}
    outputs = await submit(service, "Build a climbing app", "", "t-prefs", preferences=prefs)
    values, _interrupt, _next = await service._read("t-prefs")
    assert values["preferences"] == {"platform": "web", "stack_hints": "TypeScript + Postgres",
                                     "deploy_target": "Fly.io", "must_use": "", "must_avoid": ""}
    assert "Fly.io" in chat_text(outputs[-1])  # the chat shows what was stated


async def test_a_run_without_preferences_has_the_defaults(demo_service) -> None:
    service, _ = demo_service
    await submit(service, "Build a climbing app", "", "t-noprefs")
    values, _interrupt, _next = await service._read("t-noprefs")
    assert values["preferences"]["platform"] == "any" and not values["preferences"]["must_use"]


@pytest.fixture()
def research_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_RESEARCH", "true")  # request before `demo_service`: its settings read this


async def test_the_research_brief_shows_as_a_linked_card_before_the_panel(research_on, demo_service) -> None:
    service, _ = demo_service
    outputs = await submit(service, "Build a climbing app", "", "t-research")
    first_with_card = next(i for i, output in enumerate(outputs) if "Research brief" in chat_text(output))
    final = chat_text(outputs[-1])
    assert 'rel="noopener noreferrer"' in final and "https://demo-logbook.example.com" in final
    assert final.index("Research brief") < final.index("speaker-card speaker-pm")  # read before the panel speaks
    assert first_with_card < len(outputs) - 1  # it appeared as soon as the research step finished
    values, _interrupt, _next = await service._read("t-research")
    assert values["research"]["competitors"]


async def test_the_first_overlay_says_research_is_running_when_it_is_enabled(research_on, demo_service) -> None:
    service, _ = demo_service
    outputs = await submit(service, "Build a climbing app", "", "t-research-overlay")
    assert "Searching the web" in chat_text(outputs[0])

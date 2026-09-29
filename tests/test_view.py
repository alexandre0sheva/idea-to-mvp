from langchain_core.messages import AIMessage, HumanMessage

from idea_to_mvp.state import make_initial_state
from idea_to_mvp.ui.view import (
    MODE_ANSWERS,
    MODE_ARCH_CHOICE,
    MODE_DONE,
    MODE_IDEA,
    MODE_IMPL_GATE,
    MODE_INTERRUPTED,
    MODE_PLAN_GATE,
    ViewEntry,
    mode_from_state,
    running_from_state,
    stage_for_view,
    status_from_state,
    transcript_from_state,
)

QUESTIONS = ["1. Who is the user?", "2. What is v0?"]
ITEMS = [
    {"question": "Who is the user?", "why_it_matters": "Scope", "suggested_answer": "Solo founders"},
    {"question": "What is v0?", "why_it_matters": "Cut", "suggested_answer": "Core loop"},
]


def _values(**overrides):
    values = dict(make_initial_state("A climbing app", 1))
    values["discussion_history"] = [
        HumanMessage(content="A climbing app"),
        AIMessage(content="PM says scope small.", name="pm"),
        AIMessage(content="Tech Lead says monolith.", name="tech_lead"),
    ]
    values.update(overrides)
    return values


def _kinds(entries: list[ViewEntry]) -> list[str]:
    return [e.kind for e in entries]


def test_transcript_orders_idea_panel_summary_and_questions() -> None:
    values = _values(summary="Brief", generated_questions=QUESTIONS, questions=ITEMS, stage="summary")
    entries = transcript_from_state(values, {"kind": "answers"})
    assert _kinds(entries) == ["idea", "discussion", "discussion", "summary", "questions"]
    assert [e.speaker for e in entries[1:3]] == ["PM", "Tech Lead"]
    assert entries[-1].content == "\n".join(QUESTIONS)
    assert entries[-1].data == ITEMS


def test_human_anchor_message_is_not_duplicated_as_a_panel_turn() -> None:
    entries = transcript_from_state(_values(), None)
    assert _kinds(entries).count("idea") == 1


def test_answers_and_architecture_choice_appear_once_decided() -> None:
    values = _values(
        summary="Brief",
        generated_questions=QUESTIONS,
        user_answers="1. Solo founders.",
        architecture="## Option A",
        arch_choice={"option": "B", "notes": "scale"},
        stage="arch_choice",
    )
    entries = transcript_from_state(values, None)
    assert _kinds(entries)[-3:] == ["user_answers", "architect", "arch_decision"]
    assert entries[-1].content == "Architecture choice: Option B — scale"


def test_pending_gate_cards_come_from_the_interrupt_payload() -> None:
    values = _values(architecture="## Option A", stage="architecture")
    entries = transcript_from_state(values, {"kind": "arch_choice", "question": "Pick one?"})
    assert entries[-1] == ViewEntry("arch_choice", "Architect", "Pick one?")
    plan = transcript_from_state(_values(stage="strategy"), {"kind": "plan_gate", "question": "Generate?"})
    assert plan[-1].kind == "planner_offer"
    implement = transcript_from_state(_values(stage="plan_bundle"), {"kind": "implement_gate", "question": "Go?"})
    assert implement[-1].kind == "implement_gate"


def test_plan_and_implement_decisions_are_derived_from_stage_and_state() -> None:
    declined = transcript_from_state(
        _values(strategy=None, stage="done", plan_decision={"generate": False, "notes": ""}), None
    )
    assert declined[-1] == ViewEntry("planner_decision", "You", "Skip for now")

    started = _values(
        stage="verification",
        plan_decision={"generate": True, "notes": "lean"},
        implement_decision={"implement": True, "notes": ""},
        project_bundle_summary="Pack ready",
        implementation_log="Built it.",
        workspace_dir="/w",
    )
    kinds = _kinds(transcript_from_state(started, None))
    assert kinds[-4:] == ["planner_decision", "project_bundle", "implement_decision", "implementation"]

    at_implement_gate = _values(
        stage="plan_bundle",
        plan_decision={"generate": True, "notes": ""},
        project_bundle_summary="Pack ready",
    )
    assert "implement_decision" not in _kinds(transcript_from_state(at_implement_gate, {"kind": "implement_gate"}))


def test_verification_and_delivery_are_rendered_at_the_end() -> None:
    values = _values(
        stage="done",
        plan_decision={"generate": True, "notes": ""},
        implement_decision={"implement": True, "notes": ""},
        verification={"passed": True, "attempts": 1, "report": "all green"},
        delivery_report="## Delivery report",
    )
    entries = transcript_from_state(values, None)
    assert _kinds(entries)[-2:] == ["verification", "delivery_report"]
    assert "passed" in entries[-2].content and "1 fix attempt" in entries[-2].content


def test_overlays_replace_the_pending_gate_card() -> None:
    values = _values(architecture="## Option A", stage="architecture")
    pending = ViewEntry("arch_decision", "You", "Architecture choice: Option A")
    entries = transcript_from_state(
        values,
        {"kind": "arch_choice", "question": "Pick?"},
        running=("Strategy", "Choosing..."),
        pending_user=pending,
        error="boom",
    )
    assert _kinds(entries)[-3:] == ["arch_decision", "thinking", "system"]
    assert "arch_choice" not in _kinds(entries)


def test_mode_from_state() -> None:
    values = _values()
    assert mode_from_state({}, None) == MODE_IDEA
    assert mode_from_state(values, {"kind": "answers"}) == MODE_ANSWERS
    assert mode_from_state(values, {"kind": "arch_choice"}) == MODE_ARCH_CHOICE
    assert mode_from_state(values, {"kind": "plan_gate"}) == MODE_PLAN_GATE
    assert mode_from_state(values, {"kind": "implement_gate"}) == MODE_IMPL_GATE
    assert mode_from_state(values, None, next_nodes=()) == MODE_DONE
    assert mode_from_state(values, None, next_nodes=("implementer",)) == MODE_INTERRUPTED


def test_running_step_and_status() -> None:
    values = _values(next_speaker="Skeptic", turn_count=2, max_rounds=3)
    assert running_from_state(values, ("discussion",)) == ("Skeptic", "Skeptic is drafting the next panel turn...")
    assert running_from_state(values, ("verifier",))[0] == "Verifier"
    assert running_from_state(values, ("collect_answers",)) is None
    assert status_from_state(values, MODE_INTERRUPTED, ("Skeptic", "x")) == "Turn 2/3 complete. Next: Skeptic."
    assert "Answer the MVP" in status_from_state(values, MODE_ANSWERS)
    assert status_from_state(values, MODE_INTERRUPTED).startswith("This run was interrupted")


def test_stage_for_view() -> None:
    assert stage_for_view({}, MODE_IDEA) == "discussion"
    assert stage_for_view(_values(), MODE_PLAN_GATE) == "plan_gate"
    assert stage_for_view(_values(stage="verification"), MODE_INTERRUPTED) == "verification"
    assert stage_for_view(_values(), MODE_DONE) == "done"

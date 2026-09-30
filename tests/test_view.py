from datetime import UTC

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


def test_the_verification_result_is_shown_until_the_delivery_dashboard_replaces_it() -> None:
    values = _values(
        stage="report",
        plan_decision={"generate": True, "notes": ""},
        implement_decision={"implement": True, "notes": ""},
        verification={"passed": True, "attempts": 1, "report": "all green"},
    )
    entries = transcript_from_state(values, None)
    assert _kinds(entries)[-1] == "verification"
    assert "passed" in entries[-1].content and "1 fix attempt" in entries[-1].content


def test_the_delivery_report_is_one_entry_that_carries_the_state_for_the_dashboard() -> None:
    values = _values(
        stage="done",
        plan_decision={"generate": True, "notes": ""},
        implement_decision={"implement": True, "notes": ""},
        verification={"passed": True, "attempts": 1, "report": "all green"},
        delivery_report="## Delivery report",
    )
    entries = transcript_from_state(values, None)
    assert _kinds(entries)[-1] == "delivery_report" and "verification" not in _kinds(entries)
    assert entries[-1].content == "## Delivery report" and entries[-1].data is values


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
    assert running_from_state(values, ("panel",)) == ("Skeptic", "Skeptic is drafting the next panel turn...")
    assert running_from_state(values, ("verify",))[0] == "Verifier"
    assert running_from_state(values, ("fix",))[0] == "Fixer"
    assert running_from_state(values, ("collect_answers",)) is None
    assert status_from_state(values, MODE_INTERRUPTED, ("Skeptic", "x")) == "Turn 2/3 complete. Next: Skeptic."
    assert "Answer the MVP" in status_from_state(values, MODE_ANSWERS)
    assert status_from_state(values, MODE_INTERRUPTED).startswith("This run was interrupted")


def test_the_workspace_step_has_its_own_progress_message() -> None:
    running = running_from_state(_values(), ("prepare_workspace",))
    assert running is not None and running[0] == "Workspace"


def test_stage_for_view() -> None:
    assert stage_for_view({}, MODE_IDEA) == "discussion"
    assert stage_for_view(_values(), MODE_PLAN_GATE) == "plan_gate"
    assert stage_for_view(_values(stage="verification"), MODE_INTERRUPTED) == "verification"
    assert stage_for_view(_values(), MODE_DONE) == "done"


def test_opening_statements_are_announced_as_a_parallel_step() -> None:
    fresh = _values(turn_count=0, panel_mode="moderated")
    speaker, message = running_from_state(fresh, ("panel",))
    assert speaker == "Panel" and "opening statements" in message
    assert status_from_state(fresh, MODE_INTERRUPTED, (speaker, message)) == message
    round_robin = _values(turn_count=0, panel_mode="round_robin", next_speaker="PM")
    assert running_from_state(round_robin, ("panel",)) == ("PM", "PM is drafting the next panel turn...")


def test_the_moderators_convergence_note_is_shown_only_when_the_panel_converged() -> None:
    converged = _values(convergence={"converged": True, "reason": "All three agree on the scope."})
    entries = transcript_from_state(converged, None)
    assert _kinds(entries) == ["idea", "discussion", "discussion", "moderator"]
    assert (entries[-1].speaker, entries[-1].content) == ("Moderator", "All three agree on the scope.")
    ongoing = _values(convergence={"converged": False, "reason": "Still disagree on storage."})
    assert "moderator" not in _kinds(transcript_from_state(ongoing, None))
    assert "moderator" not in _kinds(transcript_from_state(_values(), None))


# ---------------------------------------------------------------- iteration


def test_the_iterate_gate_is_its_own_mode_with_a_delivery_card() -> None:
    from idea_to_mvp.ui.view import MODE_ITERATE_GATE

    values = _values(stage="done", plan_decision={"generate": True, "notes": ""}, delivery_report="## Delivery report")
    gate = {"kind": "iterate_gate", "question": "Want changes?", "iteration": 1}
    assert mode_from_state(values, gate) == MODE_ITERATE_GATE
    entries = transcript_from_state(values, gate)
    assert _kinds(entries)[-2:] == ["delivery_report", "iterate_gate"] and entries[-1].content == "Want changes?"
    assert "changes" in status_from_state(values, MODE_ITERATE_GATE).lower()
    assert stage_for_view(values, MODE_ITERATE_GATE) == "done"


def test_change_requests_appear_as_your_messages_before_the_new_implementation() -> None:
    values = _values(
        stage="done",
        plan_decision={"generate": True, "notes": ""},
        implement_decision={"implement": True, "notes": ""},
        change_requests=["Add CSV export.", "Make it dark."],
        iteration=3,
        implementation_log="## I3-01 — done",
        delivery_report="## Delivery report",
    )
    entries = transcript_from_state(values, None)
    requests = [e for e in entries if e.kind == "change_request"]
    assert [e.speaker for e in requests] == ["You", "You"]
    assert [e.content for e in requests] == ["Change request (v0.2): Add CSV export.", "Change request (v0.3): Make it dark."]
    assert _kinds(entries).index("change_request") < _kinds(entries).index("implementation")


def test_the_change_planner_step_has_its_own_progress_message() -> None:
    running = running_from_state(_values(), ("change_planner",))
    assert running is not None and running[0] == "Change planner"


# ------------------------------------------------------------ stage timing


def _snapshots(*rows):
    """History snapshots, newest first (as `aget_state_history` yields them): (seconds, stage, next nodes)."""
    from datetime import datetime
    from types import SimpleNamespace

    return [
        SimpleNamespace(
            created_at=datetime.fromtimestamp(1_000_000 + at, tz=UTC).isoformat(),
            values={"stage": stage} if stage else {},
            next=tuple(nxt),
        )
        for at, stage, nxt in reversed(rows)
    ]


def test_stage_marks_are_chronological_and_know_when_the_graph_waited_at_a_gate() -> None:
    from idea_to_mvp.ui.view import stage_marks

    marks = stage_marks(_snapshots((0, "discussion", ("panel",)), (30, "summary", ("collect_answers",)), (90, "answers", ("architect",))))
    assert [m.stage for m in marks] == ["discussion", "summary", "answers"]
    assert [m.at_gate for m in marks] == [False, True, False]
    assert marks[1].at - marks[0].at == 30


def test_each_step_is_credited_with_the_time_its_checkpoint_took_to_arrive() -> None:
    from idea_to_mvp.ui.view import stage_elapsed, stage_marks

    marks = stage_marks(_snapshots((0, "discussion", ("panel",)), (40, "discussion", ("summarizer",)), (55, "summary", ("collect_answers",))))
    assert stage_elapsed(marks) == {"discussion": 40.0, "summary": 15.0}


def test_time_spent_waiting_for_the_user_at_a_gate_is_not_counted() -> None:
    from idea_to_mvp.ui.view import stage_elapsed, stage_marks

    marks = stage_marks(
        _snapshots(
            (0, "summary", ("collect_answers",)),
            (600, "answers", ("architect",)),  # ten minutes at the gate
            (630, "architecture", ("arch_choice",)),
            (1000, "arch_choice", ("strategy",)),  # and again
            (1020, "strategy", ("plan_gate",)),
        )
    )
    assert stage_elapsed(marks) == {"arch_choice": 30.0, "strategy": 20.0}  # answers: 0, waiting is not work


def test_state_stages_are_credited_to_their_pipeline_step() -> None:
    from idea_to_mvp.ui.view import stage_elapsed, stage_marks

    marks = stage_marks(_snapshots((0, "strategy", ()), (5, "plan_gate", ()), (35, "report", ()), (36, "done", ())))
    elapsed = stage_elapsed(marks)
    assert elapsed["plan_bundle"] == 5.0 and elapsed["verification"] == 30.0 and elapsed["done"] == 1.0


def test_the_running_step_is_credited_with_the_time_since_the_last_checkpoint() -> None:
    from idea_to_mvp.ui.view import stage_elapsed, stage_marks

    marks = stage_marks(_snapshots((0, "summary", ("architect",)), (20, "architecture", ("strategy",))))
    now = 1_000_000 + 27
    assert stage_elapsed(marks, now=now, active="strategy") == {"arch_choice": 20.0, "strategy": 7.0}
    assert stage_elapsed(marks) == {"arch_choice": 20.0}  # not running: nothing is added


def test_no_time_is_added_while_the_graph_waits_at_a_gate() -> None:
    from idea_to_mvp.ui.view import stage_elapsed, stage_marks

    marks = stage_marks(_snapshots((0, "summary", ("summarizer",)), (20, "summary", ("collect_answers",))))
    assert stage_elapsed(marks, now=1_000_000 + 500, active="answers") == {"summary": 20.0}


def test_snapshots_without_a_stage_or_a_timestamp_are_ignored() -> None:
    from types import SimpleNamespace

    from idea_to_mvp.ui.view import stage_elapsed, stage_marks

    broken = [SimpleNamespace(created_at=None, values={"stage": "summary"}, next=()), *_snapshots((0, None, ()), (5, "summary", ()))]
    marks = stage_marks(broken)
    assert [m.stage for m in marks] == ["summary"] and stage_elapsed(marks) == {}


def test_a_failed_verification_marks_its_step_failed() -> None:
    from idea_to_mvp.ui.view import stage_statuses

    assert stage_statuses(_values(verification={"passed": False, "attempts": 2, "report": "3 failed", "lanes": []})) == {"verification": "failed"}
    assert stage_statuses(_values(verification={"passed": True, "attempts": 0, "report": "ok", "lanes": []})) == {}
    assert stage_statuses(_values()) == {}  # not verified yet

"""Headless runs: `idea-to-mvp run / resume / sessions` on the real graph and checkpointer, in demo mode."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from service_helpers import mode_of

from idea_to_mvp import cli, llm
from idea_to_mvp.config import get_settings
from idea_to_mvp.graph import ThreadView
from idea_to_mvp.headless import (
    EXIT_ERROR,
    EXIT_FAILED,
    EXIT_GATE,
    EXIT_VERIFIED,
    HeadlessOptions,
    decide,
    number_answers,
    progress_lines,
    render_summary,
    run_idea,
    summarize,
)
from idea_to_mvp.nodes.gates import AUTOPILOT_NOTE
from idea_to_mvp.sessions import SessionRegistry
from idea_to_mvp.ui.view import MODE_IMPL_GATE

IDEA = "a habit tracker"
QUESTIONS = [
    {"question": f"Q{n}?", "why_it_matters": "it matters", "suggested_answer": f"suggestion {n}"}
    for n in range(1, 6)
]


@pytest.fixture()
def cwd(demo_env: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(demo_env)  # never pick up the developer's real .env
    return demo_env


def run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict, str]:
    """(exit code, the JSON summary, stderr) of one CLI call."""
    code = cli.main([*argv, "--json"])
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


def graph_values(cwd: Path, thread: str) -> dict:
    import asyncio

    from idea_to_mvp.graph import open_graph, read_thread

    async def read() -> dict:
        async with open_graph(get_settings()) as graph:
            return (await read_thread(graph, thread)).values

    return asyncio.run(read())


# ------------------------------------------------------------------ gate decisions


def test_answers_come_from_the_file_then_the_suggestions_else_the_run_stops() -> None:
    payload = {"kind": "answers", "question_items": QUESTIONS}
    assert decide(payload, HeadlessOptions(answers="Solo climbers\n\n3. Logging only")).resume == (
        "1. Solo climbers\n3. Logging only"
    )
    autopilot = decide(payload, HeadlessOptions(autopilot=True)).resume
    assert autopilot.splitlines()[0] == "1. suggestion 1" and len(autopilot.splitlines()) == 5
    missing = [{**QUESTIONS[0], "suggested_answer": ""}, *QUESTIONS[1:]]
    stop = decide({"kind": "answers", "question_items": missing}, HeadlessOptions(autopilot=True))
    assert stop.resume is None and "--answers FILE" in stop.note


def test_number_answers_keeps_given_numbers_and_numbers_the_rest() -> None:
    assert number_answers("  a \n\n2) b\nc\n") == "1. a\n2) b\n3. c"
    assert number_answers("\n \n") == ""


def test_arch_flag_beats_the_autopilot_recommendation() -> None:
    payload = {"kind": "arch_choice", "proposal": {"recommendation": "A"}}
    assert decide(payload, HeadlessOptions(arch="B", autopilot=True)).resume["option"] == "B"
    assert decide(payload, HeadlessOptions(autopilot=True)).resume == {
        "option": "A",
        "notes": AUTOPILOT_NOTE,
    }
    assert decide(payload, HeadlessOptions()).resume is None


def test_the_pack_is_generated_with_autopilot_or_implement_only() -> None:
    assert decide({"kind": "plan_gate"}, HeadlessOptions()).resume is None
    assert decide({"kind": "plan_gate"}, HeadlessOptions(autopilot=True)).resume["generate"] is True
    assert decide({"kind": "plan_gate"}, HeadlessOptions(implement=True)).resume["generate"] is True


def test_the_implement_gate_needs_the_explicit_flag_even_on_autopilot() -> None:
    estimate = {
        "low": 1.5,
        "high": 6.0,
        "per_task": 0.5,
        "basis": "catalog",
        "samples": 0,
        "tasks": 3,
        "unpriced": [],
    }
    payload = {"kind": "implement_gate", "cost_estimate": estimate}
    stop = decide(payload, HeadlessOptions(autopilot=True))
    assert stop.resume is None and "$1.50–$6.00 for 3 task(s)" in stop.note
    assert decide(payload, HeadlessOptions(implement=True, parallel=2)).resume == {
        "implement": True,
        "notes": "Headless run: --implement.",
        "parallel": 2,
    }


def test_a_new_version_needs_feedback_and_implement() -> None:
    payload = {"kind": "iterate_gate"}
    assert decide(payload, HeadlessOptions(implement=True)).resume is None
    assert "add --implement" in decide(payload, HeadlessOptions(feedback="dark mode")).note
    assert decide(payload, HeadlessOptions(feedback="dark mode", implement=True)).resume == {
        "iterate": True,
        "feedback": "dark mode",
    }


# ------------------------------------------------------------------ summary


@pytest.mark.parametrize(
    ("values", "gate", "next_nodes", "outcome", "code"),
    [
        (
            {"user_idea": "x", "delivery_report": "r", "verification": {"passed": True}},
            {"kind": "iterate_gate"},
            (),
            "verified",
            EXIT_VERIFIED,
        ),
        (
            {"user_idea": "x", "delivery_report": "r", "verification": {"passed": False}},
            None,
            (),
            "failed",
            EXIT_FAILED,
        ),
        ({"user_idea": "x"}, {"kind": "implement_gate"}, ("implement_gate",), "gate", EXIT_GATE),
        ({"user_idea": "x"}, None, ("summarizer",), "interrupted", EXIT_ERROR),
        ({"user_idea": "x"}, None, (), "ended", EXIT_GATE),
        ({}, None, (), "error", EXIT_ERROR),
        # a later version that stopped mid-build is not delivered, whatever the previous version's report says
        (
            {"user_idea": "x", "delivery_report": "r", "verification": {"passed": True}},
            None,
            ("implementer",),
            "interrupted",
            EXIT_ERROR,
        ),
    ],
)
def test_the_exit_code_says_how_the_run_ended(
    values: dict, gate: dict | None, next_nodes: tuple, outcome: str, code: int
) -> None:
    summary = summarize("t1", ThreadView(values, gate, next_nodes))
    assert (summary.outcome, summary.exit_code) == (outcome, code)


def test_progress_names_committed_steps_and_agent_sessions() -> None:
    assert progress_lines(("panel:abc",), "updates", {"speaker_turn": {}}) == [
        "done panel/speaker_turn"
    ]
    assert progress_lines((), "updates", {"__interrupt__": ()}) == []
    start = {"kind": "task_start", "task_id": "T01", "label": "Core journey", "detail": ""}
    assert progress_lines((), "custom", start) == ["start T01 Core journey"]
    assert progress_lines((), "custom", {**start, "kind": "task_end", "detail": "done"}) == [
        "end   T01 Core journey: done"
    ]
    assert progress_lines((), "custom", {**start, "kind": "tool"}) == []


# ------------------------------------------------------------------ the CLI, end to end (demo mode)


def test_run_with_autopilot_and_implement_delivers_offline(
    cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, summary, err = run_cli(capsys, "run", IDEA, "--autopilot", "--implement")
    assert (
        code == EXIT_VERIFIED
        and summary["outcome"] == "verified"
        and summary["verification"] == "passed"
    )
    assert (
        summary["gate"] == "iterate_gate"
    )  # delivered; the session can still take a change request
    assert (
        Path(summary["workspace"]).is_relative_to(cwd / "projects")
        and Path(summary["delivery_zip"]).is_file()
    )
    assert summary["agent_cost_usd"] == 0.0 and summary["total_tokens"] > 0
    assert (
        "done summarizer" in err
        and "start T01" in err
        and "gate  implement_gate: implement=True" in err
    )

    code = cli.main(["sessions", "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert (
        code == 0
        and rows[0]["thread_id"] == summary["thread_id"]
        and rows[0]["waiting_at"] == "iterate_gate"
    )


def test_without_implement_the_run_stops_at_the_gate_with_the_estimate_then_resumes(
    cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["run", IDEA, "--autopilot"])
    out = capsys.readouterr().out
    assert code == EXIT_GATE
    assert (
        "stopped at a gate: implement_gate" in out and "Estimate:" in out and "--implement" in out
    )
    assert not (cwd / "projects").exists()  # no agent session ran
    thread = next(line.split()[-1] for line in out.splitlines() if line.startswith("Session:"))

    code, summary, _err = run_cli(capsys, "resume", thread, "--implement", "--parallel", "1")
    assert code == EXIT_VERIFIED and summary["thread_id"] == thread
    assert graph_values(cwd, thread)["implement_decision"]["parallel"] == 1


def test_every_gate_is_answered_by_its_own_flag(
    cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, summary, _err = run_cli(capsys, "run", IDEA)
    assert code == EXIT_GATE and summary["gate"] == "answers" and len(summary["questions"]) == 5
    assert all(item["suggested_answer"] for item in summary["questions"])
    thread = summary["thread_id"]

    answers = cwd / "answers.txt"
    answers.write_text("Solo climbers\nLogging only\n", encoding="utf-8")
    code, summary, _err = run_cli(capsys, "resume", thread, "--answers", str(answers))
    assert code == EXIT_GATE and summary["gate"] == "arch_choice"
    assert set(summary["architecture_options"]) == {"A", "B", "recommended"}

    code, summary, _err = run_cli(capsys, "resume", thread, "--arch", "2")
    assert code == EXIT_GATE and summary["gate"] == "plan_gate"
    values = graph_values(cwd, thread)
    assert values["user_answers"] == "1. Solo climbers\n2. Logging only"
    assert values["arch_choice"]["option"] == "B" and values["autopilot"] is False

    code, summary, _err = run_cli(capsys, "resume", thread, "--implement")
    assert code == EXIT_VERIFIED


def test_feedback_builds_the_next_version_only_with_implement(
    cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _code, summary, _err = run_cli(capsys, "run", IDEA, "--autopilot", "--implement")
    thread = summary["thread_id"]

    code, summary, _err = run_cli(capsys, "resume", thread, "--feedback", "add a streak counter")
    assert (
        code == EXIT_VERIFIED
        and summary["iteration"] == 1
        and "add --implement" in summary["message"]
    )

    code, summary, _err = run_cli(
        capsys, "resume", thread, "--feedback", "add a streak counter", "--implement"
    )
    assert code == EXIT_VERIFIED and summary["iteration"] == 2 and summary["gate"] == "iterate_gate"
    assert graph_values(cwd, thread)["change_requests"] == [
        "add a streak counter"
    ]  # used once, not per version


def test_max_total_usd_overrides_the_budget_setting(
    cwd: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMPLEMENTER_MAX_TOTAL_USD", "25")  # restored after the test
    code = cli.main(["run", IDEA, "--autopilot", "--max-total-usd", "3.5", "--json"])
    capsys.readouterr()
    assert code == EXIT_GATE and get_settings().implementer_max_total_usd == 3.5


def test_a_failed_step_exits_1_and_resume_continues_from_the_checkpoint(
    cwd: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("provider exploded")

    with monkeypatch.context() as patch:
        patch.setattr(llm, "invoke_text", broken)
        code, summary, _err = run_cli(capsys, "run", IDEA)
    assert (
        code == EXIT_ERROR
        and summary["outcome"] == "error"
        and "provider exploded" in summary["message"]
    )
    assert summary["next_command"] == f"idea-to-mvp resume {summary['thread_id']}"

    code, summary, _err = run_cli(capsys, "resume", summary["thread_id"])
    assert code == EXIT_GATE and summary["gate"] == "answers"


def test_resume_of_an_unknown_session_is_an_error(
    cwd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, summary, _err = run_cli(capsys, "resume", "no-such-thread")
    assert code == EXIT_ERROR and "idea-to-mvp sessions" in summary["message"]


def test_sessions_lists_nothing_yet(cwd: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["sessions"]) == 0 and capsys.readouterr().out.strip() == "No saved sessions."


@pytest.mark.parametrize(
    "argv", [["run"], ["run", IDEA, "--arch", "3"], ["run", IDEA, "--max-total-usd", "0"]]
)
def test_usage_errors_exit_1_not_2(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(argv)
    assert raised.value.code == 1


def test_an_empty_answers_file_is_refused(cwd: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (cwd / "empty.txt").write_text("\n", encoding="utf-8")
    assert cli.main(["run", IDEA, "--answers", str(cwd / "empty.txt")]) == 1
    assert cli.main(["run", IDEA, "--answers", str(cwd / "missing.txt")]) == 1
    assert "--answers" in capsys.readouterr().err


def test_the_text_summary_lists_the_questions_and_the_next_command() -> None:
    summary = summarize(
        "t1",
        ThreadView(
            {"user_idea": "x"},
            {"kind": "answers", "question_items": QUESTIONS},
            ("collect_answers",),
        ),
    )
    text = render_summary(summary)
    assert "1. Q1?" in text and "suggested: suggestion 1" in text
    assert "Next:      idea-to-mvp resume t1 --answers FILE" in text


# ------------------------------------------------------------------ same graph as the UI


async def test_a_headless_session_is_the_same_thread_the_ui_shows(demo_service) -> None:
    service, graphs = demo_service
    settings = get_settings()
    registry = SessionRegistry(settings.checkpoint_path)
    lines: list[str] = []
    summary = await run_idea(
        await graphs.get(),
        registry,
        IDEA,
        HeadlessOptions(autopilot=True),
        settings=settings,
        progress=lines.append,
        thread_id="shared",
    )
    assert summary.exit_code == EXIT_GATE and summary.gate == "implement_gate"
    assert await mode_of(service, "shared") == MODE_IMPL_GATE
    assert [s.interrupted_at for s in registry.list() if s.thread_id == "shared"] == [
        "implement_gate"
    ]
    assert lines[0] == "session shared"

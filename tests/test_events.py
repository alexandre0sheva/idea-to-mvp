import time

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from idea_to_mvp.implementation.events import events_from_sdk_message, make_event
from idea_to_mvp.ui.view import implementation_progress


def assistant(*blocks) -> AssistantMessage:
    return AssistantMessage(content=list(blocks), model="claude-x")


def test_a_tool_use_becomes_a_tool_event_with_the_file_it_touches() -> None:
    message = assistant(ToolUseBlock(id="1", name="Write", input={"file_path": "src/app.py", "content": "x" * 5000}))
    (event,) = events_from_sdk_message(message, "T01")
    assert event["kind"] == "tool" and event["label"] == "Write"
    assert event["detail"] == "src/app.py" and event["task_id"] == "T01"
    assert event["cost_usd"] is None and isinstance(event["ts"], float)
    assert abs(event["ts"] - time.time()) < 5


def test_tool_details_come_from_the_most_telling_input_and_are_truncated() -> None:
    bash = assistant(ToolUseBlock(id="1", name="Bash", input={"command": "pytest -q " + "x" * 400}))
    (event,) = events_from_sdk_message(bash, None)
    assert event["detail"].startswith("pytest -q") and len(event["detail"]) <= 160 and event["detail"].endswith("…")
    grep = assistant(ToolUseBlock(id="2", name="Grep", input={"pattern": "TODO", "path": "src"}))
    assert events_from_sdk_message(grep, None)[0]["detail"] == "TODO"
    unknown = assistant(ToolUseBlock(id="3", name="Mystery", input={"weird": 1}))
    assert events_from_sdk_message(unknown, None)[0]["detail"] == ""


def test_text_becomes_a_text_event_and_blank_text_and_thinking_are_ignored() -> None:
    message = assistant(
        TextBlock(text="  Creating the core module.  "),
        TextBlock(text="   "),
        ThinkingBlock(thinking="private reasoning", signature="s"),
    )
    events = events_from_sdk_message(message, "T02")
    assert [(e["kind"], e["detail"]) for e in events] == [("text", "Creating the core module.")]


def test_several_blocks_keep_their_order() -> None:
    message = assistant(
        TextBlock(text="Plan"),
        ToolUseBlock(id="1", name="Read", input={"file_path": "a.py"}),
        ToolUseBlock(id="2", name="Edit", input={"file_path": "b.py"}),
    )
    assert [e["label"] for e in events_from_sdk_message(message, None)] == ["agent", "Read", "Edit"]


def test_the_result_message_reports_the_sessions_cost() -> None:
    result = ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=7, session_id="s", total_cost_usd=0.4321
    )
    (event,) = events_from_sdk_message(result, "T01")
    assert event["kind"] == "cost" and event["cost_usd"] == 0.4321 and "7 turns" in event["detail"]
    free = ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="s")
    assert events_from_sdk_message(free, None) == []  # no cost reported, nothing to show


def test_other_messages_produce_no_events() -> None:
    assert events_from_sdk_message(UserMessage(content=[ToolResultBlock(tool_use_id="1", content="ok")]), None) == []
    assert events_from_sdk_message(SystemMessage(subtype="init", data={}), None) == []
    assert events_from_sdk_message("not a message", None) == []


def test_make_event_fills_the_contract() -> None:
    event = make_event("task_start", task_id="T03", label="Title", detail="3/5")
    assert set(event) == {"kind", "task_id", "label", "detail", "cost_usd", "ts"}
    assert event["cost_usd"] is None and event["kind"] == "task_start"


# ------------------------------------------------ the status line derived from events


def ev(kind, **kw):
    return make_event(kind, **kw)


def test_progress_shows_the_current_task_the_last_action_and_the_running_total() -> None:
    events = [
        ev("task_start", task_id="T01", label="Project setup", detail="1/3"),
        ev("tool", task_id="T01", label="Write", detail="src/app.py"),
        ev("cost", task_id="T01", label="session", cost_usd=0.40),
        ev("task_end", task_id="T01", label="Project setup", detail="done"),
        ev("task_start", task_id="T02", label="Core journey", detail="2/3"),
        ev("tool", task_id="T02", label="Bash", detail="pytest -q"),
    ]
    status, console = implementation_progress(events, total_budget=25.0)
    assert status == "Implementing T02 (2/3): Core journey · Bash pytest -q · $0.40 of $25.00"
    lines = console.splitlines()
    assert lines[0].startswith("T01") and "done" in lines[0]  # finished tasks are summarised
    assert any("Bash" in line and "pytest -q" in line for line in lines)


def test_progress_before_any_task_and_with_no_events() -> None:
    assert implementation_progress([], total_budget=25.0) == ("", "")
    status, _ = implementation_progress([ev("task_start", task_id="T01", label="Setup", detail="")], total_budget=10.0)
    assert status.startswith("Implementing T01: Setup") and "$0.00 of $10.00" in status


def test_progress_keeps_only_the_recent_actions_in_the_console() -> None:
    events = [ev("task_start", task_id="T01", label="Setup", detail="1/1")]
    events += [ev("tool", task_id="T01", label="Write", detail=f"file{i}.py") for i in range(30)]
    _, console = implementation_progress(events, total_budget=5.0)
    assert "file29.py" in console and "file0.py" not in console and console.count("\n") < 12


def test_a_failed_task_is_marked_in_the_console() -> None:
    events = [
        ev("task_start", task_id="T01", label="Setup", detail="1/2"),
        ev("task_end", task_id="T01", label="Setup", detail="failed: budget exhausted"),
    ]
    _, console = implementation_progress(events, total_budget=5.0)
    assert "failed: budget exhausted" in console


def test_parallel_tasks_are_reported_together() -> None:
    events = [
        ev("task_start", task_id="T02", label="Left"),
        ev("task_start", task_id="T03", label="Right"),
        ev("tool", task_id="T02", label="Write", detail="left.py"),
        ev("tool", task_id="T03", label="Bash", detail="pytest -q"),
        ev("cost", task_id="T02", label="session", cost_usd=1.0),
    ]
    status, console = implementation_progress(events, total_budget=25.0)
    assert status == "Implementing 2 tasks in parallel (T02 Left, T03 Right) · Bash pytest -q · $1.00 of $25.00"
    lines = console.splitlines()
    assert "T02 Left — working" in lines and "T03 Right — working" in lines
    assert "  Write left.py" in lines and "  Bash pytest -q" in lines


def test_a_task_finishing_while_another_still_runs_switches_back_to_the_single_task_view() -> None:
    events = [
        ev("task_start", task_id="T02", label="Left"),
        ev("task_start", task_id="T03", label="Right"),
        ev("task_end", task_id="T02", label="Left", detail="done"),
        ev("tool", task_id="T03", label="Write", detail="right.py"),
    ]
    status, console = implementation_progress(events, total_budget=5.0)
    assert status.startswith("Implementing T03: Right · Write right.py")
    assert console.splitlines()[0] == "T02 Left: done"


def test_verification_lanes_are_reported_as_verifying_not_implementing() -> None:
    events = [
        ev("task_start", task_id="verify:tests", label="Tests lane"),
        ev("task_start", task_id="verify:quality", label="Quality lane"),
        ev("task_start", task_id="verify:requirements", label="Requirements lane"),
        ev("tool", task_id="verify:tests", label="Bash", detail="pytest -q"),
    ]
    status, console = implementation_progress(events, total_budget=25.0)
    assert status == "Verifying 3 lanes in parallel (Tests lane, Quality lane, Requirements lane) · Bash pytest -q · $0.00 of $25.00"
    assert "Tests lane — working" in console.splitlines() and "verify:" not in console


def test_a_single_lane_and_the_fix_session_are_worded_for_what_they_do() -> None:
    running = [ev("task_start", task_id="verify:quality", label="Quality lane")]
    assert implementation_progress(running, total_budget=5.0)[0].startswith("Verifying: Quality lane")
    fixing = [ev("task_start", task_id="verify:fix", label="Fix", detail="2 failure(s)")]
    assert implementation_progress(fixing, total_budget=5.0)[0].startswith("Fixing (2 failure(s))")
    done = [*fixing, ev("task_end", task_id="verify:fix", label="Fix", detail="done")]
    status, console = implementation_progress(done, total_budget=5.0)
    assert status.startswith("Fixing: Fix finished") and console == "Fix: done"

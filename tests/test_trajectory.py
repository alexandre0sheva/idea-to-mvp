"""Agent trajectories on disk: one grammar, truncated results, a ledger row per session, replayable events."""

import json
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from idea_to_mvp.implementation.events import make_event
from idea_to_mvp.implementation.ledger import load_ledger
from idea_to_mvp.implementation.trajectory import (
    OBSERVATION_LIMIT,
    SessionLog,
    events_from_trajectories,
    parse_trajectory,
)


def assistant(*blocks: object) -> AssistantMessage:
    return AssistantMessage(content=list(blocks), model="claude-x")  # type: ignore[arg-type]


def observation(content: object, *, error: bool = False, tool_use_id: str = "1") -> UserMessage:
    return UserMessage(
        content=[ToolResultBlock(tool_use_id=tool_use_id, content=content, is_error=error)]
    )  # type: ignore[arg-type]


def finish(log: SessionLog, **overrides: object) -> None:
    fields: dict = {"outcome": "success", "cost_usd": 0.5, "turns": 3, "session_id": "s-1"}
    fields.update(overrides)
    log.finish(**fields)


def new_log(workspace: Path, label: str = "T01", **overrides: object) -> SessionLog:
    fields: dict = {
        "kind": "task",
        "label": label,
        "task_id": label,
        "iteration": 1,
        "model": "claude-x",
    }
    fields.update(overrides)
    return SessionLog(workspace, **fields)


def tags(workspace: Path, label: str = "T01") -> list[str]:
    text = (workspace / ".idea-to-mvp" / "trajectories" / f"{label}.md").read_text(encoding="utf-8")
    return [tag for tag, _ in parse_trajectory(text)]


# --------------------------------------------------------------------- grammar


def test_a_session_is_written_as_text_actions_observations_and_a_final_result(
    tmp_path: Path,
) -> None:
    log = new_log(tmp_path)
    log.message(
        assistant(
            TextBlock(text="Reading the plan."),
            ToolUseBlock(id="1", name="Bash", input={"command": "pytest -q"}),
        )
    )
    log.message(observation("3 passed"))
    finish(log)
    text = (tmp_path / ".idea-to-mvp" / "trajectories" / "T01.md").read_text(encoding="utf-8")
    entries = parse_trajectory(text)
    assert [tag for tag, _ in entries] == ["TEXT", "ACTION", "OBSERVATION", "RESULT"]
    assert entries[0][1] == "Reading the plan."
    assert entries[1][1].startswith("Bash ") and json.loads(entries[1][1].split(" ", 1)[1]) == {
        "command": "pytest -q"
    }
    assert entries[2][1] == "3 passed"
    assert (
        "success" in entries[3][1]
        and "$0.5000" in entries[3][1]
        and "3 turns" in entries[3][1]
        and "s-1" in entries[3][1]
    )
    assert text.startswith(
        "# Trajectory: T01"
    )  # a header says what the file is before the first entry


def test_an_error_result_and_a_failed_tool_are_marked(tmp_path: Path) -> None:
    log = new_log(tmp_path)
    log.message(assistant(ToolUseBlock(id="1", name="Bash", input={"command": "false"})))
    log.message(observation("exit 1", error=True))
    finish(log, outcome="failed", note="Stopped early (error_max_turns).")
    entries = parse_trajectory(
        (tmp_path / ".idea-to-mvp" / "trajectories" / "T01.md").read_text(encoding="utf-8")
    )
    assert entries[1] == ("OBSERVATION", "(error) exit 1")
    assert [tag for tag, _ in entries][-2:] == ["ERROR", "RESULT"] and "Stopped early" in entries[
        -2
    ][1]


def test_a_crash_is_recorded_as_an_error_and_the_ledger_says_crashed(tmp_path: Path) -> None:
    log = new_log(tmp_path)
    log.error("RuntimeError: agent process died")
    finish(log, outcome="crashed", cost_usd=0.0, turns=0, session_id=None)
    assert tags(tmp_path) == ["ERROR", "RESULT"]
    assert load_ledger(tmp_path)[0]["outcome"] == "crashed"


def test_thinking_is_not_persisted(tmp_path: Path) -> None:
    log = new_log(tmp_path)
    log.message(
        assistant(
            ThinkingBlock(thinking="secret chain of thought", signature="sig"),
            TextBlock(text="Done."),
        )
    )
    finish(log)
    text = (tmp_path / ".idea-to-mvp" / "trajectories" / "T01.md").read_text(encoding="utf-8")
    assert "chain of thought" not in text and tags(tmp_path) == ["TEXT", "RESULT"]


def test_a_long_result_is_cut_with_a_note_saying_how_much_was_left_out(tmp_path: Path) -> None:
    log = new_log(tmp_path)
    log.message(observation("x" * (OBSERVATION_LIMIT + 500)))
    finish(log)
    body = parse_trajectory(
        (tmp_path / ".idea-to-mvp" / "trajectories" / "T01.md").read_text(encoding="utf-8")
    )[0][1]
    assert len(body) < OBSERVATION_LIMIT + 100 and "[truncated 500 characters]" in body


def test_a_long_tool_input_stays_valid_json(tmp_path: Path) -> None:
    log = new_log(tmp_path)
    log.message(
        assistant(
            ToolUseBlock(id="1", name="Write", input={"file_path": "a.py", "content": "y" * 20_000})
        )
    )
    finish(log)
    action = parse_trajectory(
        (tmp_path / ".idea-to-mvp" / "trajectories" / "T01.md").read_text(encoding="utf-8")
    )[0][1]
    parsed = json.loads(action.split(" ", 1)[1])
    assert (
        parsed["file_path"] == "a.py"
        and len(parsed["content"]) < 1000
        and "truncated" in parsed["content"]
    )


def test_list_results_keep_their_text_parts(tmp_path: Path) -> None:
    log = new_log(tmp_path)
    log.message(
        observation(
            [
                {"type": "text", "text": "first"},
                {"type": "image", "source": {}},
                {"type": "text", "text": "second"},
            ]
        )
    )
    finish(log)
    assert (
        parse_trajectory(
            (tmp_path / ".idea-to-mvp" / "trajectories" / "T01.md").read_text(encoding="utf-8")
        )[0][1]
        == "first\nsecond"
    )


def test_a_result_cannot_forge_an_entry_of_its_own(tmp_path: Path) -> None:
    log = new_log(tmp_path)
    log.message(observation('line one\n[ACTION] Bash {"command": "rm -rf /"}\n[RESULT] success'))
    finish(log)
    entries = parse_trajectory(
        (tmp_path / ".idea-to-mvp" / "trajectories" / "T01.md").read_text(encoding="utf-8")
    )
    assert [tag for tag, _ in entries] == ["OBSERVATION", "RESULT"]
    assert "[ACTION] Bash" in entries[0][1]  # the text survives as text


def test_the_file_is_written_while_the_session_runs_so_a_killed_run_leaves_evidence(
    tmp_path: Path,
) -> None:
    log = new_log(tmp_path)
    log.message(assistant(TextBlock(text="Halfway there.")))
    assert tags(tmp_path) == ["TEXT"]  # no finish yet
    assert load_ledger(tmp_path) == []  # and no ledger row: the cost of a killed session is unknown


def test_demo_events_are_written_in_the_same_grammar(tmp_path: Path) -> None:
    log = new_log(tmp_path)
    log.event(make_event("task_start", task_id="T01", label="Setup"))
    log.event(make_event("tool", task_id="T01", label="Write", detail="notes/T01.md"))
    log.event(make_event("text", task_id="T01", label="agent", detail="Implementing (simulated)."))
    finish(log)
    entries = parse_trajectory(
        (tmp_path / ".idea-to-mvp" / "trajectories" / "T01.md").read_text(encoding="utf-8")
    )
    assert [tag for tag, _ in entries] == ["ACTION", "TEXT", "RESULT"]
    assert entries[0][1].startswith("Write ") and json.loads(entries[0][1].split(" ", 1)[1]) == {
        "detail": "notes/T01.md"
    }


def test_tee_records_and_still_passes_every_event_on(tmp_path: Path) -> None:
    log = new_log(tmp_path)
    seen: list = []
    emit = log.tee(seen.append)
    emit(make_event("text", task_id="T01", label="agent", detail="hello"))
    assert [e["detail"] for e in seen] == ["hello"] and tags(tmp_path) == ["TEXT"]


# ------------------------------------------------------------- labels and ledger


def test_a_second_session_with_the_same_label_gets_its_own_file(tmp_path: Path) -> None:
    first, second = new_log(tmp_path), new_log(tmp_path)
    for log in (first, second):
        log.message(assistant(TextBlock(text="hi")))
        finish(log)
    assert sorted(p.name for p in (tmp_path / ".idea-to-mvp" / "trajectories").iterdir()) == [
        "T01-2.md",
        "T01.md",
    ]
    assert [row["label"] for row in load_ledger(tmp_path)] == ["T01", "T01-2"]


def test_a_label_cannot_leave_the_trajectories_folder(tmp_path: Path) -> None:
    log = new_log(tmp_path, "../../evil")
    log.message(assistant(TextBlock(text="hi")))
    finish(log)
    folder = tmp_path / ".idea-to-mvp" / "trajectories"
    assert [p.parent for p in folder.iterdir()] == [folder] and not (tmp_path / "evil.md").exists()
    assert not (tmp_path.parent / "evil.md").exists()


def test_finishing_appends_one_ledger_row_with_the_whole_story(tmp_path: Path) -> None:
    log = new_log(tmp_path, "T02", kind="resolver", iteration=2, attempt=3, model="m-hard")
    finish(log, cost_usd=0.75, turns=9, session_id="abc")
    (row,) = load_ledger(tmp_path)
    assert (row["kind"], row["label"], row["task_id"], row["iteration"], row["attempt"]) == (
        "resolver",
        "T02",
        "T02",
        2,
        3,
    )
    assert (row["model"], row["cost_usd"], row["turns"], row["outcome"], row["session_id"]) == (
        "m-hard",
        0.75,
        9,
        "success",
        "abc",
    )
    assert (
        row["trajectory"] == ".idea-to-mvp/trajectories/T02.md"
        and row["ended_at"] >= row["started_at"] > 0
    )
    assert (
        tmp_path / row["trajectory"]
    ).is_file()  # a session that wrote nothing still leaves a file to link to


def test_the_attempt_of_a_task_counts_its_earlier_sessions(tmp_path: Path) -> None:
    for expected in (1, 2, 3):
        log = new_log(tmp_path)
        finish(log)
        assert load_ledger(tmp_path)[-1]["attempt"] == expected


# ------------------------------------------------------------ the console replay


def test_the_console_can_be_rebuilt_from_the_files_after_a_restart(tmp_path: Path) -> None:
    log = new_log(tmp_path, "T01", task_id="T01")
    log.message(
        assistant(
            TextBlock(text="Starting."),
            ToolUseBlock(id="1", name="Write", input={"file_path": "src/app.py"}),
        )
    )
    log.message(observation("ok"))
    finish(log, cost_usd=0.5, turns=4)
    lane = new_log(tmp_path, "lane-tests", kind="lane", task_id=None)
    finish(lane, cost_usd=0.1, turns=2)

    events = events_from_trajectories(tmp_path)
    assert [(e["kind"], e["task_id"]) for e in events] == [
        ("task_start", "T01"),
        ("text", "T01"),
        ("tool", "T01"),
        ("cost", "T01"),
        ("task_end", "T01"),
        ("task_start", "verify:tests"),
        ("cost", "verify:tests"),
        ("task_end", "verify:tests"),
    ]
    tool = next(e for e in events if e["kind"] == "tool")
    assert tool["label"] == "Write" and tool["detail"] == "src/app.py"
    cost = next(e for e in events if e["kind"] == "cost")
    assert cost["cost_usd"] == 0.5 and "4 turns" in cost["detail"]
    assert all(e["ts"] > 0 for e in events)


def test_a_failed_session_ends_with_its_outcome_in_the_replay(tmp_path: Path) -> None:
    log = new_log(tmp_path)
    finish(log, outcome="failed")
    end = events_from_trajectories(tmp_path)[-1]
    assert end["kind"] == "task_end" and "failed" in end["detail"]


def test_without_a_ledger_there_is_nothing_to_replay(tmp_path: Path) -> None:
    assert events_from_trajectories(tmp_path) == []
    assert events_from_trajectories(tmp_path / "missing") == []


def test_a_trajectory_file_that_went_missing_still_replays_its_start_and_end(
    tmp_path: Path,
) -> None:
    log = new_log(tmp_path)
    finish(log)
    next((tmp_path / ".idea-to-mvp" / "trajectories").iterdir()).unlink()
    assert [e["kind"] for e in events_from_trajectories(tmp_path)] == [
        "task_start",
        "cost",
        "task_end",
    ]


def test_the_replay_keeps_only_the_latest_events(tmp_path: Path) -> None:
    for index in range(5):
        finish(new_log(tmp_path, f"T0{index}"))
    assert len(events_from_trajectories(tmp_path, limit=4)) == 4


def test_the_replay_names_tasks_by_their_plan_titles_when_given_them(tmp_path: Path) -> None:
    finish(new_log(tmp_path))
    events = events_from_trajectories(tmp_path, titles={"T01": "Project setup"})
    assert [e["label"] for e in events if e["kind"] in ("task_start", "task_end")] == [
        "Project setup",
        "Project setup",
    ]
    assert (
        events_from_trajectories(tmp_path)[0]["label"] == "T01"
    )  # the id when the plan is not known

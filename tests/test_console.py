"""The implementation view: task board, live console, cost meter (pure renderers over fabricated data)."""

import re

from plan_helpers import make_plan, make_task

from idea_to_mvp.implementation.events import make_event
from idea_to_mvp.implementation.progress import TaskResult
from idea_to_mvp.ui.console import render_console, render_cost_meter, render_task_board


def result(task_id: str, status: str = "done", cost: float = 0.5, **overrides: object) -> TaskResult:
    fields: dict = {"task_id": task_id, "status": status, "summary": f"Built {task_id}.", "cost_usd": cost, "turns": 4, "session_id": None, "commit": None}
    fields.update(overrides)
    return fields  # type: ignore[return-value]


def plan_dict() -> dict:
    plan = make_plan(
        [
            make_task("T01", title="Setup"),
            make_task("T02", title="Left", depends_on=["T01"]),
            make_task("T03", title="Right", depends_on=["T01"]),
            make_task("T04", title="Join", depends_on=["T02", "T03"]),
        ]
    )
    return plan.model_dump()


def columns(html: str) -> dict[str, list[str]]:
    """column name -> ids of the task cards in it."""
    found: dict[str, list[str]] = {}
    for name, body in re.findall(r"<section class='board-col' data-col='(\w+)'>(.*?)</section>", html, flags=re.DOTALL):
        found[name] = re.findall(r"data-task='([^']+)'", body)
    return found


# --------------------------------------------------------------------- board


def test_tasks_sit_in_the_column_of_their_state() -> None:
    results = {"T01": result("T01"), "T02": result("T02", "failed", summary="Stopped early.")}
    html = render_task_board(plan_dict(), results, running={"T03"})
    assert columns(html) == {"pending": ["T04"], "running": ["T03"], "done": ["T01"], "failed": ["T02"]}


def test_a_task_moves_across_the_board_as_it_runs_and_finishes() -> None:
    plan = plan_dict()
    assert columns(render_task_board(plan, {}, set()))["pending"] == ["T01", "T02", "T03", "T04"]
    assert columns(render_task_board(plan, {}, {"T01"}))["running"] == ["T01"]
    assert columns(render_task_board(plan, {"T01": result("T01")}, set()))["done"] == ["T01"]


def test_a_finished_task_is_done_even_if_a_late_start_event_still_says_running() -> None:
    """The board reads results from disk and the running set from the event stream; the stream can lag behind,
    and a task that already finished must not flicker back to running when its start event arrives."""
    cols = columns(render_task_board(plan_dict(), {"T01": result("T01")}, {"T01"}))
    assert cols["done"] == ["T01"] and cols["running"] == []


def test_a_task_that_is_being_retried_counts_as_running_even_with_an_old_failure() -> None:
    cols = columns(render_task_board(plan_dict(), {"T02": result("T02", "failed")}, {"T02"}))
    assert cols["running"] == ["T02"] and cols["failed"] == []


def test_skipped_tasks_are_listed_as_failed_and_say_so() -> None:
    html = render_task_board(plan_dict(), {"T04": result("T04", "skipped", summary="Skipped: depends on T02.")}, set())
    assert columns(html)["failed"] == ["T04"] and "skipped" in html.lower()


def test_cards_show_title_workstream_cost_and_the_branch() -> None:
    html = render_task_board(plan_dict(), {"T02": result("T02", cost=1.25, commit="abc1234")}, set(), parallel=True)
    card = html.split("data-task='T02'")[1].split("</article>")[0]
    assert "Left" in card and "backend-api" in card and "$1.25" in card and "task/T02" in card and "abc1234" in card
    sequential = render_task_board(plan_dict(), {"T02": result("T02")}, set(), parallel=False)
    assert "task/T02" not in sequential and "main" in sequential


def test_each_card_expands_to_the_agents_summary_and_the_diff_stat() -> None:
    results = {"T01": result("T01", summary="Set up the repo.\nNext tasks: use the CLI.", commit="abc1234")}
    html = render_task_board(plan_dict(), results, set(), diffs={"abc1234": " src/app.py | 12 ++++\n 1 file changed, 12 insertions(+)"})
    card = html.split("data-task='T01'")[1].split("</article>")[0]
    assert "<details" in card and "Set up the repo." in card and "src/app.py | 12" in card and "1 file changed" in card


def test_everything_from_the_agents_is_escaped() -> None:
    evil = "<script>alert(1)</script>"
    plan = make_plan([make_task("T01", title=evil, workstream="<b>x</b>")]).model_dump()
    html = render_task_board(plan, {"T01": result("T01", summary=evil, commit="abc1234")}, set(), diffs={"abc1234": evil})
    assert "<script>" not in html and "<b>x</b>" not in html and "&lt;script&gt;" in html


def test_a_board_without_a_plan_explains_itself() -> None:
    assert "plan" in render_task_board({}, {}, set()).lower() and "data-task" not in render_task_board({"tasks": []}, {}, set())


def test_column_headers_count_their_tasks() -> None:
    html = render_task_board(plan_dict(), {"T01": result("T01")}, {"T02", "T03"})
    assert "Running (2)" in html and "Done (1)" in html and "Pending (1)" in html and "Failed (0)" in html


# ------------------------------------------------------------------- console


def test_the_console_lists_events_oldest_first_with_who_and_what() -> None:
    events = [
        make_event("task_start", task_id="T01", label="Setup", detail="1/3"),
        make_event("tool", task_id="T01", label="Write", detail="src/app.py"),
        make_event("text", task_id="T01", label="agent", detail="Creating the module."),
        make_event("cost", task_id="T01", label="session", detail="4 turns", cost_usd=0.42),
        make_event("task_end", task_id="T01", label="Setup", detail="done"),
    ]
    html = render_console(events)
    lines = re.findall(r"<div class='console-line (\w+)'>(.*?)</div>", html, flags=re.DOTALL)
    assert [kind for kind, _ in lines] == ["task_start", "tool", "text", "cost", "task_end"]
    assert "Write" in lines[1][1] and "src/app.py" in lines[1][1] and "T01" in lines[1][1]
    assert "$0.42" in lines[3][1] and "Creating the module." in lines[2][1]


def test_the_console_is_collapsible_and_scrolls_to_the_newest_line() -> None:
    html = render_console([make_event("text", label="agent", detail="hi")])
    assert "<details class='console'" in html and " open" in html.split(">")[0] + ">"
    assert "console-scroll" in html and "role='log'" in html and "aria-live='polite'" in html


def test_only_the_latest_events_up_to_the_limit_are_shown_and_the_cut_is_announced() -> None:
    events = [make_event("tool", task_id="T01", label="Write", detail=f"file{i}.py") for i in range(30)]
    html = render_console(events, limit=10)
    assert "file29.py" in html and "file20.py" in html and "file19.py" not in html
    assert "last 10 of 30" in html
    assert "last" not in render_console(events[:5], limit=10).split("</summary>")[0]


def test_event_text_is_escaped() -> None:
    html = render_console([make_event("tool", task_id="<i>", label="<b>Bash</b>", detail="<script>alert(1)</script>")])
    assert "<script>" not in html and "<b>Bash</b>" not in html and "&lt;script&gt;" in html


def test_an_empty_console_says_it_is_waiting() -> None:
    assert "Waiting" in render_console([])


# --------------------------------------------------------------------- meter


def test_the_cost_meter_shows_spend_against_the_budget() -> None:
    html = render_cost_meter(6.25, 25.0)
    assert "$6.25" in html and "$25.00" in html and "width: 25%" in html and "role='meter'" in html


def test_the_meter_warns_as_the_budget_runs_out_and_never_overflows() -> None:
    assert "cost-meter warn" in render_cost_meter(21.0, 25.0)
    over = render_cost_meter(30.0, 25.0)
    assert "cost-meter over" in over and "width: 100%" in over
    assert "cost-meter warn" not in render_cost_meter(5.0, 25.0) and "cost-meter over" not in render_cost_meter(5.0, 25.0)


def test_a_meter_without_a_budget_shows_only_the_spend() -> None:
    html = render_cost_meter(1.5, 0)
    assert "$1.50" in html and "width:" not in html


# ----------------------------------------------------------- running tasks


def test_running_tasks_are_those_started_and_not_yet_ended() -> None:
    from idea_to_mvp.ui.console import running_tasks

    events = [
        make_event("task_start", task_id="T01"),
        make_event("task_start", task_id="T02"),
        make_event("task_end", task_id="T01", detail="done"),
        make_event("tool", task_id="T02", label="Write"),
        make_event("task_start", task_id="verify:tests"),  # a verification lane is not a plan task
        make_event("task_start", task_id=None),
    ]
    assert running_tasks(events) == {"T02"}
    assert running_tasks(events + [make_event("task_end", task_id="T02")]) == set()
    assert running_tasks(events + [make_event("task_start", task_id="T01")]) == {"T01", "T02"}  # started again

"""The delivery dashboard: verdict per lane, requirements coverage, cost and time, how to run."""

import re
from pathlib import Path
from typing import Any

from plan_helpers import PRD, make_plan, make_task

from idea_to_mvp.plan import ProjectCommands
from idea_to_mvp.state import make_initial_state
from idea_to_mvp.ui.dashboard import render_dashboard, run_instructions

README = "# App\n\nIntro.\n\n## Run\n\n```bash\nnpm install\nnpm start\n```\n\n## Test\n\n```bash\nnpm test\n```\n"


def lane(name: str, passed: bool = True, failures: list[str] | None = None, **fields: Any) -> dict[str, Any]:
    return {
        "lane": name,
        "passed": passed,
        "commands_run": [{"command": f"check {name}", "exit_code": 0 if passed else 1}],
        "failures": failures if failures is not None else ([] if passed else [f"{name} broke"]),
        "summary": f"{name} summary",
        **fields,
    }


def state(tmp_path: Path, *, lanes: list[dict] | None = None, passed: bool = True, results: dict | None = None, **fields: Any) -> dict[str, Any]:
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    plan = make_plan(
        [
            make_task("T01", requirement_ids=["R1", "R2"], workstream="backend-api"),
            make_task("T02", requirement_ids=["R1"], workstream="web-ui", depends_on=["T01"]),
        ]
    )
    (workspace / "plan.json").write_text(plan.model_dump_json())
    (workspace / "PRD.md").write_text(PRD)
    (workspace / "README.md").write_text(README)
    values: dict[str, Any] = dict(make_initial_state("idea", 1))
    values.update(
        workspace_dir=str(workspace),
        iteration=1,
        verification={
            "passed": passed,
            "attempts": 1,
            "report": "r",
            "lanes": lanes if lanes is not None else [lane("tests"), lane("quality"), lane("requirements")],
        },
        task_results=results
        if results is not None
        else {
            "T01": {"task_id": "T01", "status": "done", "summary": "s", "cost_usd": 1.0, "turns": 3, "session_id": None, "commit": None},
            "T02": {"task_id": "T02", "status": "done", "summary": "s", "cost_usd": 0.5, "turns": 3, "session_id": None, "commit": None},
        },
    )
    values.update(fields)
    return values


def cards(html: str) -> dict[str, str]:
    """lane -> its status class."""
    return {lane_name: status for status, lane_name in re.findall(r"<section class='lane-card (\w+)' data-lane='(\w+)'>", html)}


# ------------------------------------------------------------------- verdicts


def test_the_dashboard_has_one_verdict_card_per_lane(tmp_path: Path) -> None:
    lanes = [lane("tests"), lane("quality", False, ["ruff: 2 errors"]), lane("requirements")]
    html = render_dashboard(state(tmp_path, lanes=lanes, passed=False))
    assert cards(html) == {"tests": "passed", "quality": "failed", "requirements": "passed"}
    assert "ruff: 2 errors" in html and "check quality" in html and "quality summary" in html
    assert html.count("PASSED") >= 2 and "FAILED" in html


def test_the_header_names_the_version_and_the_overall_verdict(tmp_path: Path) -> None:
    html = render_dashboard(state(tmp_path, iteration=2))
    assert "Delivery report" in html and "v0.2" in html and "1 fix attempt" in html
    failed = render_dashboard(state(tmp_path, passed=False, lanes=[lane("tests", False)]))
    assert "dashboard-verdict failed" in failed and "dashboard-verdict passed" in html


def test_a_verification_that_never_ran_shows_its_report_instead_of_lane_cards(tmp_path: Path) -> None:
    values = state(tmp_path, lanes=[], passed=False)
    values["verification"]["report"] = "Verification was not run: the whole-run budget is used up."
    html = render_dashboard(values)
    assert cards(html) == {} and "budget is used up" in html


# --------------------------------------------------------------- requirements


def rows(html: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for row in re.findall(r"<tr class='req-row (\w+)' data-req='(\w+)'>(.*?)</tr>", html, flags=re.DOTALL):
        found[row[1]] = [row[0], *re.findall(r"<td>(.*?)</td>", row[2], flags=re.DOTALL)]
    return found


def test_the_requirements_table_shows_priority_tasks_and_status(tmp_path: Path) -> None:
    found = rows(render_dashboard(state(tmp_path)))
    assert found["R1"][0] == "delivered" and "P0" in found["R1"][1] and "T01" in found["R1"][2] and "T02" in found["R1"][2]
    assert found["R2"][0] == "delivered" and "T01" in found["R2"][2]
    assert found["R3"][0] == "uncovered" and "P1" in found["R3"][1]


def test_a_requirement_whose_task_failed_is_incomplete_and_one_the_lane_flags_is_not_tested(tmp_path: Path) -> None:
    results = {
        "T01": {"task_id": "T01", "status": "failed", "summary": "s", "cost_usd": 0.0, "turns": 1, "session_id": None, "commit": None},
        "T02": {"task_id": "T02", "status": "done", "summary": "s", "cost_usd": 0.0, "turns": 1, "session_id": None, "commit": None},
    }
    lanes = [lane("tests"), lane("quality"), lane("requirements", False, ["R1 (P0): no test covers the log flow"])]
    found = rows(render_dashboard(state(tmp_path, results=results, lanes=lanes, passed=False)))
    assert found["R2"][0] == "incomplete"  # its only task failed
    assert found["R1"][0] == "untested"  # covered by tasks, but the requirements lane found no test


def test_without_a_prd_there_is_no_requirements_table(tmp_path: Path) -> None:
    values = state(tmp_path)
    (Path(values["workspace_dir"]) / "PRD.md").unlink()
    assert "req-row" not in render_dashboard(values)


# ------------------------------------------------------------------ cost/time


def test_cost_tokens_tasks_and_time_are_summarised(tmp_path: Path) -> None:
    usage = [
        {"role": "pm", "provider": "x", "model": "m", "input_tokens": 9000, "output_tokens": 3300, "cost_usd": None},
        {"role": "implementer", "provider": "anthropic", "model": "m", "input_tokens": 0, "output_tokens": 0, "cost_usd": 1.204},
    ]
    html = render_dashboard(state(tmp_path, usage=usage), elapsed={"discussion": 60.0, "implementation": 240.0})
    assert "$1.20" in html and "12.3k tokens" in html and "5m 00s" in html and "2 of 2 tasks" in html


def test_time_is_unknown_without_timings(tmp_path: Path) -> None:
    html = render_dashboard(state(tmp_path))
    assert "stat-time" in html and "—" in html.split("stat-time")[1].split("</div>")[0]


# ---------------------------------------------------------------- how to run


def test_how_to_run_uses_the_plans_commands_first(tmp_path: Path) -> None:
    values = state(tmp_path)
    plan = make_plan(commands=ProjectCommands(install="pip install -e .", test="pytest", lint=None, run="python -m app"))
    (Path(values["workspace_dir"]) / "plan.json").write_text(plan.model_dump_json())
    html = render_dashboard(values)
    assert "python -m app" in html and "pip install -e ." in html


def test_how_to_run_falls_back_to_the_readmes_run_section(tmp_path: Path) -> None:
    html = render_dashboard(state(tmp_path))  # the default plan has no run command
    assert "npm install" in html and "npm start" in html and "npm test" not in html.split("How to run")[1].split("</section>")[0]


def test_how_to_run_points_at_the_readme_when_nothing_can_be_extracted(tmp_path: Path) -> None:
    values = state(tmp_path)
    (Path(values["workspace_dir"]) / "README.md").write_text("# App\nNo instructions.\n")
    assert "README.md" in render_dashboard(values).split("How to run")[1]


def test_run_instructions_are_labelled_commands(tmp_path: Path) -> None:
    values = state(tmp_path)
    assert run_instructions(Path(values["workspace_dir"]))[0] == ("README", "npm install\nnpm start")


# ------------------------------------------------------------------- the rest


def test_the_delivery_zip_and_workspace_are_shown(tmp_path: Path) -> None:
    html = render_dashboard(state(tmp_path, delivery_zip=str(tmp_path / "deliveries" / "ws-v0.1.zip")))
    assert "ws-v0.1.zip" in html and str(tmp_path / "ws") in html


def test_everything_the_agents_wrote_is_escaped(tmp_path: Path) -> None:
    evil = "<script>alert(1)</script>"
    lanes = [lane("tests", False, [evil], summary=evil), lane("quality"), lane("requirements")]
    values = state(tmp_path, lanes=lanes, passed=False)
    (Path(values["workspace_dir"]) / "README.md").write_text(f"## Run\n\n```\n{evil}\n```\n")
    html = render_dashboard(values)
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_an_empty_state_still_renders(tmp_path: Path) -> None:
    html = render_dashboard(dict(make_initial_state("idea", 1)))
    assert "Delivery report" in html

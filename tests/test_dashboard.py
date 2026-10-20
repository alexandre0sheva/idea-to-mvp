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


# ------------------------------------------------- planning cost, and estimate vs actual


def stat_value(html: str, name: str) -> str:
    return re.search(rf"stat-{name}'><span class='stat-label'>[^<]*</span><span class='stat-value'>([^<]*)<", html).group(1)  # type: ignore[union-attr]


def test_planning_and_agent_spend_are_separate_figures(tmp_path: Path) -> None:
    usage = [
        {"role": "pm", "provider": "x", "model": "m", "input_tokens": 9000, "output_tokens": 3300, "cost_usd": 0.25},
        {"role": "architect", "provider": "x", "model": "m", "input_tokens": 100, "output_tokens": 100, "cost_usd": 0.5},
        {"role": "implementer", "provider": "anthropic", "model": "m", "input_tokens": 0, "output_tokens": 0, "cost_usd": 1.204},
    ]
    html = render_dashboard(state(tmp_path, usage=usage))
    assert stat_value(html, "planning") == "$0.75" and stat_value(html, "cost") == "$1.20"


def test_planning_calls_without_a_price_are_called_unpriced(tmp_path: Path) -> None:
    usage = [{"role": "pm", "provider": "x", "model": "mystery", "input_tokens": 90, "output_tokens": 30, "cost_usd": None}]
    assert stat_value(render_dashboard(state(tmp_path, usage=usage)), "planning") == "unpriced"
    partly = usage + [{"role": "architect", "provider": "x", "model": "m", "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.5}]
    assert stat_value(render_dashboard(state(tmp_path, usage=partly)), "planning") == "$0.50 + unpriced calls"


ESTIMATE = {"low": 1.0, "high": 3.0, "per_task": 0.5, "basis": "catalog", "samples": 0, "tasks": 2, "unpriced": []}
SPENT = [{"role": "implementer", "provider": "anthropic", "model": "m", "input_tokens": 0, "output_tokens": 0, "cost_usd": 1.204}]


def test_the_dashboard_sets_the_gates_estimate_beside_what_was_spent(tmp_path: Path) -> None:
    value = stat_value(render_dashboard(state(tmp_path, usage=SPENT, cost_estimate=ESTIMATE)), "estimate")
    assert "$1.00–$3.00" in value and "$1.20" in value and "within" in value


def test_an_overrun_is_called_an_overrun(tmp_path: Path) -> None:
    over = [{**SPENT[0], "cost_usd": 4.5}]
    assert "over" in stat_value(render_dashboard(state(tmp_path, usage=over, cost_estimate=ESTIMATE)), "estimate")
    under = [{**SPENT[0], "cost_usd": 0.4}]
    assert "under" in stat_value(render_dashboard(state(tmp_path, usage=under, cost_estimate=ESTIMATE)), "estimate")


def test_a_tuned_runs_estimate_says_it_includes_the_pilot_runs(tmp_path: Path) -> None:
    pilot = [*SPENT, {**SPENT[0], "role": "pilot", "cost_usd": 1.5}]  # $2.70 in all, within $1.00–$3.00
    value = stat_value(render_dashboard(state(tmp_path, usage=pilot, cost_estimate={**ESTIMATE, "harness_tuning": True})), "estimate")
    assert "estimated (incl. harness tuning)" in value and "$2.70" in value and "within" in value


def test_there_is_no_estimate_figure_without_an_estimate_or_after_the_first_version(tmp_path: Path) -> None:
    assert "stat-estimate" not in render_dashboard(state(tmp_path, usage=SPENT))
    assert "stat-estimate" not in render_dashboard(state(tmp_path, usage=SPENT, cost_estimate=ESTIMATE, iteration=2))
    unpriced = {**ESTIMATE, "low": None, "high": None, "basis": "unpriced"}
    assert "unpriced" in stat_value(render_dashboard(state(tmp_path, usage=SPENT, cost_estimate=unpriced)), "estimate")


# ------------------------------------------------------------- test evidence


def card_of_the_tests_lane(html: str) -> str:
    return re.search(r"<section class='lane-card \w+' data-lane='tests'>.*?</section>", html, flags=re.DOTALL).group(0)  # type: ignore[union-attr]


def test_the_card_of_the_tests_lane_shows_the_counts_parsed_from_the_report(tmp_path: Path) -> None:
    evidence = {"tests": 12, "passed": 9, "failed": 2, "errors": 0, "skipped": 1, "coverage_percent": 87.3}
    lanes = [lane("tests", False, ["2 test(s) failed"], evidence=evidence), lane("quality"), lane("requirements")]
    card = card_of_the_tests_lane(render_dashboard(state(tmp_path, lanes=lanes, passed=False)))
    assert "9 passed" in card and "2 failed" in card and "1 skipped" in card and "87% coverage" in card
    assert "from the test report" in card


def test_errors_count_with_the_failures_and_a_missing_coverage_is_left_out(tmp_path: Path) -> None:
    evidence = {"tests": 3, "passed": 1, "failed": 1, "errors": 1, "skipped": 0, "coverage_percent": None}
    card = card_of_the_tests_lane(render_dashboard(state(tmp_path, lanes=[lane("tests", False, evidence=evidence)], passed=False)))
    assert "2 failed" in card and "coverage" not in card


def test_a_tests_lane_without_a_report_says_it_is_the_agents_word(tmp_path: Path) -> None:
    card = card_of_the_tests_lane(render_dashboard(state(tmp_path, lanes=[lane("tests")])))
    assert "agent-reported" in card and "passed" in card


def test_the_other_lanes_show_no_test_counts(tmp_path: Path) -> None:
    html = render_dashboard(state(tmp_path, lanes=[lane("tests"), lane("quality"), lane("requirements")]))
    assert html.count("agent-reported") == 1


# ------------------------------------------------------------------- test integrity


def integrity_finding(status: str = "open", **fields: Any) -> dict[str, Any]:
    return {
        "id": "v1:fix-1:skip_added:tests/test_core.py:", "session": "fix-1", "iteration": 1, "kind": "skip_added",
        "path": "tests/test_core.py", "block": "", "before": 0, "status": status, "reason": "", "stale": "",
        "detail": "fix-1 added 1 skip / xfail / only marker(s) to tests/test_core.py.", **fields,
    }  # fmt: skip


def integrity_card(html: str) -> str:
    match = re.search(r"<section class='lane-card integrity \w+'.*?</section>", html, flags=re.DOTALL)
    return match.group(0) if match else ""


def test_an_open_integrity_finding_is_a_blocked_card(tmp_path: Path) -> None:
    findings = {"a": integrity_finding()}
    html = render_dashboard(state(tmp_path, passed=False, integrity_findings=findings, verification={"passed": False, "attempts": 2, "report": "r", "lanes": [], "integrity": "block"}))
    card = integrity_card(html)
    assert "BLOCKED" in card and "1 open" in card and "tests/test_core.py" in card and "lane-card integrity failed" in card


def test_a_justified_change_shows_the_agents_reason_and_says_whose_words_they_are(tmp_path: Path) -> None:
    findings = {"a": integrity_finding("justified", reason="<b>R3</b> changed")}
    card = integrity_card(render_dashboard(state(tmp_path, integrity_findings=findings, verification={"passed": True, "attempts": 1, "report": "r", "lanes": [], "integrity": "block"})))
    assert "CLEAR" in card and "justified by the agent" in card and "&lt;b&gt;R3&lt;/b&gt; changed" in card
    assert "quality/TEST_CHANGES.md" in card and "review them" in card


def test_warn_mode_calls_an_open_finding_a_warning(tmp_path: Path) -> None:
    card = integrity_card(render_dashboard(state(tmp_path, integrity_findings={"a": integrity_finding()}, verification={"passed": True, "attempts": 0, "report": "r", "lanes": [], "integrity": "warn"})))
    assert "WARNING" in card


def test_a_clean_check_is_one_line_and_an_unchecked_run_says_nothing(tmp_path: Path) -> None:
    checked = render_dashboard(state(tmp_path, verification={"passed": True, "attempts": 0, "report": "r", "lanes": [], "integrity": "block"}))
    assert "no session deleted, renamed, skipped, or weakened a test" in checked and not integrity_card(checked)
    unchecked = render_dashboard(state(tmp_path, integrity_findings={"a": integrity_finding()}))  # a verdict from before the guard
    assert "Test integrity" not in unchecked
    off = render_dashboard(state(tmp_path, verification={"passed": True, "attempts": 0, "report": "r", "lanes": [], "integrity": "off"}))
    assert "Test integrity" not in off


# ------------------------------------------------------------------- held-out acceptance tests


def test_the_acceptance_lane_has_a_card_and_each_requirement_its_acceptance_verdict(tmp_path: Path) -> None:
    evidence = {"tests": 3, "passed": 2, "failed": 1, "errors": 0, "skipped": 0, "requirements": {"R1": "passed", "R2": "failed"}, "suite": "/out/acceptance/ws/v1"}
    lanes = [lane("tests"), lane("quality"), lane("requirements"), lane("acceptance", False, ["a::test_R2_x: R2 broke"], evidence=evidence)]
    html = render_dashboard(state(tmp_path, lanes=lanes, passed=False))
    assert cards(html)["acceptance"] == "failed"
    assert "2 passed, 1 failed, 0 skipped (held-out acceptance tests" in html and "/out/acceptance/ws/v1" in html
    assert "<th>Acceptance</th>" in html
    assert re.search(r"data-req='R1'>.*?<td class='req-acceptance passed'>passed</td>", html)
    assert re.search(r"data-req='R2'>.*?<td class='req-acceptance failed'>failed</td>", html)
    assert re.search(r"data-req='R3'>.*?<td class='req-acceptance no-test'>no test</td>", html)


def test_without_an_acceptance_lane_the_table_has_no_acceptance_column(tmp_path: Path) -> None:
    assert "Acceptance" not in render_dashboard(state(tmp_path))


# ------------------------------------------------------------- fix attempt history


def fix_entry(attempt: int, **fields: Any) -> dict[str, Any]:
    return {
        "attempt": attempt, "model": "claude-sonnet-5-5", "kind": "code", "summary": "Changed the parser.",
        "failures": ["[tests] test_a failed"], "files": ["src/parser.py"], "cost_usd": 0.5,
        "notes": [{"verdict": "new", "hypothesis": "the parser drops the year", "evidence": "report: test_leap fails"}],
        "notes_problem": "", "result": "failed", "remaining": 1, **fields,
    }  # fmt: skip


def history_card(html: str) -> str:
    match = re.search(r"<section class='lane-card fix-history[^']*'.*?</section>", html, flags=re.DOTALL)
    return match.group(0) if match else ""


def test_the_attempt_history_shows_each_attempt_with_its_files_hypothesis_and_result(tmp_path: Path) -> None:
    history = [fix_entry(1), fix_entry(2, result="passed", remaining=0, notes=[{"verdict": "dropped", "hypothesis": "the parser drops the year", "evidence": "fix-1 run"}])]
    verification = {"passed": True, "attempts": 2, "report": "r", "lanes": [lane("tests")], "history": history}
    card = history_card(render_dashboard(state(tmp_path, verification=verification)))
    assert "Fix attempts" in card and "Attempt 1" in card and "Attempt 2" in card
    assert "src/parser.py" in card and "the parser drops the year" in card and "fix-1 run" in card
    assert "still failing" in card and "verification passed" in card and "claude-sonnet-5-5" in card


def test_a_run_without_fix_attempts_has_no_history_card(tmp_path: Path) -> None:
    assert history_card(render_dashboard(state(tmp_path))) == ""


def test_a_loop_that_stopped_for_the_environment_says_what_is_missing(tmp_path: Path) -> None:
    history = [fix_entry(1, kind="environment", result="stopped", files=[], notes=[], summary="Nothing changed.")]
    verification = {"passed": False, "attempts": 1, "report": "r", "lanes": [lane("tests", False)], "history": history, "environment": "The sandbox has no <docker>."}
    card = history_card(render_dashboard(state(tmp_path, passed=False, verification=verification)))
    assert "environment" in card.lower() and "The sandbox has no &lt;docker&gt;." in card and "stopped" in card
    assert "lane-card fix-history failed" in card


def test_what_agents_wrote_in_the_history_is_escaped(tmp_path: Path) -> None:
    history = [fix_entry(1, files=["<script>x</script>.py"], notes=[{"verdict": "new", "hypothesis": "<img src=x>", "evidence": "<b>e</b>"}], notes_problem="<i>late</i>")]
    verification = {"passed": False, "attempts": 1, "report": "r", "lanes": [], "history": history}
    html = render_dashboard(state(tmp_path, passed=False, verification=verification))
    assert "<script>" not in html and "<img src=x>" not in html and "&lt;img src=x&gt;" in html and "&lt;i&gt;late" in html

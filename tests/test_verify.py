"""Verification lanes: structured reports (never silently FAIL), the combined verdict, lane sessions."""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ResultMessage
from plan_helpers import PRD, make_plan

from idea_to_mvp.config import Settings
from idea_to_mvp.implementation import options as opts
from idea_to_mvp.implementation.events import ImplEvent
from idea_to_mvp.implementation.verify import (
    LANES,
    LaneReport,
    build_lane_prompt,
    combine_lanes,
    failure_list,
    parse_lane_report,
    run_fix,
    run_lane,
)


def report(lane: str = "tests", **overrides: Any) -> LaneReport:
    fields: dict[str, Any] = {
        "lane": lane,
        "passed": True,
        "commands_run": [{"command": "pytest", "exit_code": 0}],
        "failures": [],
        "summary": "All good.",
    }
    fields.update(overrides)
    return LaneReport(**fields)


def make_settings(**overrides: Any) -> Settings:
    fields: dict[str, Any] = {"implementer_sandbox": "off", "verifier_max_turns": 9}
    fields.update(overrides)
    return Settings(_env_file=None, **fields)


@pytest.fixture(autouse=True)
def sandbox_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(opts, "sandbox_supported", lambda *a, **k: True)


# ------------------------------------------------------------ parse_lane_report


def test_a_valid_structured_result_becomes_the_lane_report() -> None:
    structured = report("quality", passed=False, failures=["ruff: 3 errors"]).model_dump()
    parsed = parse_lane_report("quality", structured, "")
    assert parsed.passed is False and parsed.failures == ["ruff: 3 errors"] and parsed.lane == "quality"


def test_a_json_report_in_the_text_is_accepted_even_inside_a_code_fence() -> None:
    text = "Here is my report:\n```json\n" + json.dumps(report().model_dump()) + "\n```"
    assert parse_lane_report("tests", None, text).passed is True


def test_a_reply_that_is_not_json_fails_the_lane_with_an_explicit_reason() -> None:
    parsed = parse_lane_report("tests", None, "Everything looks fine to me.\nVERDICT: PASS")
    assert parsed.passed is False and parsed.lane == "tests"
    assert "did not return a valid report" in parsed.failures[0] and "tests" in parsed.failures[0]
    assert "VERDICT: PASS" in parsed.summary  # what the agent said is kept for the human


def test_an_empty_reply_fails_with_a_reason_too() -> None:
    parsed = parse_lane_report("requirements", None, "")
    assert parsed.passed is False and "no report" in parsed.failures[0].lower()


def test_json_that_does_not_match_the_schema_fails_with_the_validation_error() -> None:
    parsed = parse_lane_report("tests", {"passed": True}, "")
    assert parsed.passed is False and "did not return a valid report" in parsed.failures[0]


def test_a_report_for_another_lane_is_rejected() -> None:
    parsed = parse_lane_report("tests", report("quality").model_dump(), "")
    assert parsed.passed is False and "quality" in parsed.failures[0]


def test_a_lane_that_claims_to_pass_while_listing_failures_does_not_pass() -> None:
    parsed = parse_lane_report("tests", report(passed=True, failures=["test_x failed"]).model_dump(), "")
    assert parsed.passed is False and parsed.failures == ["test_x failed"]


# ---------------------------------------------------------------- combine_lanes


def test_the_verdict_needs_every_lane_to_pass() -> None:
    reports = [report("tests"), report("quality"), report("requirements")]
    result = combine_lanes(reports)
    assert result["passed"] is True and [lane["lane"] for lane in result["lanes"]] == list(LANES)
    reports[1] = report("quality", passed=False, failures=["mypy: 2 errors"])
    assert combine_lanes(reports)["passed"] is False


def test_a_lane_that_never_reported_fails_the_verdict() -> None:
    result = combine_lanes([report("tests"), report("quality")])
    assert result["passed"] is False
    missing = next(lane for lane in result["lanes"] if lane["lane"] == "requirements")
    assert missing["passed"] is False and "did not report" in missing["failures"][0]


def test_the_combined_report_gives_a_verdict_per_lane_and_lists_the_failures() -> None:
    result = combine_lanes(
        [
            report("tests", summary="12 passed"),
            report("quality", passed=False, failures=["ruff: unused import"], summary="lint fails"),
            report("requirements", passed=False, failures=["R2 (P0): no test covers it"], summary="one gap"),
        ]
    )
    text = result["report"]
    assert "Tests lane: PASSED" in text and "Quality lane: FAILED" in text and "Requirements lane: FAILED" in text
    assert "R2 (P0): no test covers it" in text and "12 passed" in text and "`pytest` (exit 0)" in text


def test_failures_are_merged_into_one_list_tagged_with_their_lane() -> None:
    lanes = combine_lanes(
        [report("tests", passed=False, failures=["test_a failed", "test_b failed"]), report("quality"), report("requirements")]
    )["lanes"]
    assert failure_list(lanes) == ["[tests] test_a failed", "[tests] test_b failed"]


# ---------------------------------------------------------------- lane prompts


def test_the_tests_lane_prompt_names_the_plans_install_and_test_commands() -> None:
    prompt = build_lane_prompt("tests", make_plan(), PRD)
    assert "pip install -e ." in prompt and "pytest" in prompt


def test_the_quality_lane_prompt_asks_for_a_smoke_probe_of_the_documented_run_command() -> None:
    prompt = build_lane_prompt("quality", make_plan(), PRD).lower()
    assert "smoke" in prompt and "readme" in prompt


def test_the_requirements_lane_prompt_lists_the_p0_requirements_and_not_the_others() -> None:
    prompt = build_lane_prompt("requirements", make_plan(), PRD)
    assert "R1" in prompt and "R2" in prompt and "R3" not in prompt


def test_prompts_work_without_a_plan_or_a_prd() -> None:
    for lane in LANES:
        assert build_lane_prompt(lane, None, "").strip()


# ---------------------------------------------------------------- lane sessions


class FakeLaneQuery:
    """Stands in for the SDK's `query`: ends with a result carrying structured output, text, or an error."""

    def __init__(self, *, structured: Any = None, text: str = "", error: bool = False, crash: bool = False) -> None:
        self.structured = structured
        self.text = text
        self.error = error
        self.crash = crash
        self.calls: list[tuple[str, Any]] = []

    async def __call__(self, *, prompt: str, options: Any) -> AsyncIterator[Any]:
        self.calls.append((prompt, options))
        if self.crash:
            raise RuntimeError("agent process died")
        yield ResultMessage(
            subtype="error_max_turns" if self.error else "success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=self.error,
            num_turns=6,
            session_id="s",
            total_cost_usd=0.75,
            result=self.text,
            structured_output=self.structured,
        )


def run_one(lane: str, query: FakeLaneQuery, workspace: Path, **kwargs: Any) -> Any:
    events: list[ImplEvent] = []
    outcome = asyncio.run(
        run_lane(workspace, lane, make_settings(), budget_usd=2.0, emit=events.append, query_fn=query, **kwargs)
    )
    return outcome, events


def test_a_lane_session_asks_the_sdk_for_a_structured_lane_report(tmp_path: Path) -> None:
    query = FakeLaneQuery(structured=report("tests").model_dump())
    outcome, _ = run_one("tests", query, tmp_path)
    options = query.calls[0][1]
    assert options.output_format == {"type": "json_schema", "schema": LaneReport.model_json_schema()}
    assert options.max_turns == 9 and options.max_budget_usd == 2.0 and options.cwd == str(tmp_path)
    assert outcome.report.passed is True and outcome.cost_usd == 0.75 and outcome.turns == 6


def test_only_the_tests_lane_may_write_files(tmp_path: Path) -> None:
    disallowed = {}
    for lane in LANES:
        query = FakeLaneQuery(structured=report(lane).model_dump())
        run_one(lane, query, tmp_path)
        disallowed[lane] = set(query.calls[0][1].disallowed_tools)
    assert {"Edit", "MultiEdit", "Write"}.isdisjoint(disallowed["tests"])
    assert {"Edit", "MultiEdit", "Write"} <= disallowed["quality"] & disallowed["requirements"]
    assert {"WebFetch", "WebSearch"} <= disallowed["tests"]  # the base restrictions stay


def test_a_lane_session_uses_the_plan_and_prd_of_the_workspace(tmp_path: Path) -> None:
    (tmp_path / "PRD.md").write_text(PRD)
    (tmp_path / "plan.json").write_text(make_plan().model_dump_json())
    query = FakeLaneQuery(structured=report("requirements").model_dump())
    run_one("requirements", query, tmp_path)
    assert "R1" in query.calls[0][0] and "R3" not in query.calls[0][0]


def test_a_reply_without_a_valid_report_fails_the_lane_but_keeps_the_cost(tmp_path: Path) -> None:
    outcome, _ = run_one("tests", FakeLaneQuery(text="All tests pass!\nVERDICT: PASS"), tmp_path)
    assert outcome.report.passed is False and "did not return a valid report" in outcome.report.failures[0]
    assert outcome.cost_usd == 0.75


def test_an_error_result_fails_the_lane_and_names_the_subtype(tmp_path: Path) -> None:
    outcome, _ = run_one("tests", FakeLaneQuery(error=True, text="out of turns"), tmp_path)
    assert outcome.report.passed is False and "error_max_turns" in outcome.report.failures[0]


def test_a_crashing_session_fails_the_lane_instead_of_raising(tmp_path: Path) -> None:
    outcome, _ = run_one("quality", FakeLaneQuery(crash=True), tmp_path)
    assert outcome.report.passed is False and "agent process died" in outcome.report.failures[0]


def test_a_lane_announces_itself_and_its_end_as_events(tmp_path: Path) -> None:
    _, events = run_one("tests", FakeLaneQuery(structured=report("tests").model_dump()), tmp_path)
    assert events[0]["kind"] == "task_start" and events[0]["task_id"] == "verify:tests"
    assert events[-1]["kind"] == "task_end" and events[-1]["task_id"] == "verify:tests"
    assert "passed" in events[-1]["detail"]


def test_demo_lanes_need_no_sdk_and_check_the_generated_demo_project(tmp_path: Path) -> None:
    from idea_to_mvp.demo.implementer import demo_implement

    demo_implement(tmp_path, {"workstreams": []})
    (tmp_path / "PRD.md").write_text(PRD)
    (tmp_path / "plan.json").write_text(make_plan().model_dump_json())
    settings = make_settings(demo_mode=True)
    outcomes = [
        asyncio.run(run_lane(tmp_path, lane, settings, budget_usd=1.0, emit=lambda e: None)) for lane in LANES
    ]
    assert [o.report.passed for o in outcomes] == [True, True, True]
    assert outcomes[0].report.commands_run and outcomes[0].report.commands_run[0]["exit_code"] == 0
    assert outcomes[2].cost_usd == 0.0


def test_the_demo_requirements_lane_reports_p0_requirements_no_task_covers(tmp_path: Path) -> None:
    (tmp_path / "PRD.md").write_text(PRD + "- R4 (P0): share a climb\n")
    (tmp_path / "plan.json").write_text(make_plan().model_dump_json())  # covers R1 and R2 only
    outcome = asyncio.run(
        run_lane(tmp_path, "requirements", make_settings(demo_mode=True), budget_usd=1.0, emit=lambda e: None)
    )
    assert outcome.report.passed is False and any("R4" in failure for failure in outcome.report.failures)


def test_the_demo_tests_lane_fails_when_the_projects_tests_fail(tmp_path: Path) -> None:
    from idea_to_mvp.demo.implementer import demo_implement

    demo_implement(tmp_path, {"workstreams": []})
    (tmp_path / "tests" / "test_broken.py").write_text(
        "import unittest\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        self.assertTrue(False)\n"
    )
    outcome = asyncio.run(run_lane(tmp_path, "tests", make_settings(demo_mode=True), budget_usd=1.0, emit=lambda e: None))
    assert outcome.report.passed is False and outcome.report.commands_run[0]["exit_code"] != 0


# ------------------------------------------------------------------- fix session


def test_the_fix_session_gets_the_merged_failure_list_not_the_raw_report(tmp_path: Path) -> None:
    query = FakeLaneQuery(text="Fixed the import.")
    events: list[ImplEvent] = []
    outcome = asyncio.run(
        run_fix(
            tmp_path,
            ["[tests] test_a failed", "[quality] ruff: unused import"],
            make_settings(implementer_max_task_turns=11),
            budget_usd=3.0,
            emit=events.append,
            query_fn=query,
        )
    )
    prompt, options = query.calls[0]
    assert "- [tests] test_a failed" in prompt and "- [quality] ruff: unused import" in prompt
    assert options.max_turns == 11 and options.max_budget_usd == 3.0 and options.output_format is None
    assert outcome.success is True and outcome.cost_usd == 0.75 and "Fixed the import." in outcome.summary


def test_a_failed_fix_session_is_reported_not_raised(tmp_path: Path) -> None:
    outcome = asyncio.run(
        run_fix(tmp_path, ["x"], make_settings(), budget_usd=1.0, emit=lambda e: None, query_fn=FakeLaneQuery(crash=True))
    )
    assert outcome.success is False and "agent process died" in outcome.summary


def test_the_demo_fix_session_changes_nothing(tmp_path: Path) -> None:
    outcome = asyncio.run(
        run_fix(tmp_path, ["x"], make_settings(demo_mode=True), budget_usd=1.0, emit=lambda e: None)
    )
    assert outcome.success is True and outcome.cost_usd == 0.0

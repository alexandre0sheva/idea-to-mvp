"""Deterministic test evidence: the orchestrator parses the JUnit / coverage files the test command wrote,
and a lane's own claim never outranks them."""

from pathlib import Path

import pytest

from idea_to_mvp.implementation.reports import (
    REPORTS_DIR,
    ReportedTests,
    apply_evidence,
    clear_reports,
    evidence_failures,
    read_evidence,
)
from idea_to_mvp.implementation.verify import LaneReport

PYTEST_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests">
  <testsuite name="pytest" errors="0" failures="1" skipped="1" tests="4">
    <testcase classname="tests.test_core" name="test_add" time="0.01"/>
    <testcase classname="tests.test_core" name="test_blank" time="0.01"/>
    <testcase classname="tests.test_core" name="test_slow" time="0.00"><skipped message="needs a gpu"/></testcase>
    <testcase classname="tests.test_core" name="test_total" time="0.02">
      <failure message="assert 3 == 4">AssertionError: assert 3 == 4</failure>
    </testcase>
  </testsuite>
</testsuites>
"""

VITEST_XML = """<?xml version="1.0" encoding="UTF-8" ?>
<testsuites name="vitest tests" tests="2" failures="0" errors="0" time="0.1">
  <testsuite name="src/a.test.ts" tests="1" failures="0"><testcase classname="src/a.test.ts" name="a works"/></testsuite>
  <testsuite name="src/b.test.ts" tests="1" failures="0"><testcase classname="src/b.test.ts" name="b works"/></testsuite>
</testsuites>
"""

COBERTURA_XML = """<?xml version="1.0" ?>
<coverage version="7.6" line-rate="0.8731" branch-rate="0" lines-valid="100" lines-covered="87">
  <packages/>
</coverage>
"""


def write_reports(workspace: Path, **files: str) -> Path:
    folder = workspace / REPORTS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (folder / name.replace("_", ".", 1)).write_text(text, encoding="utf-8")
    return folder


def lane(passed: bool = True, failures: list[str] | None = None) -> LaneReport:
    return LaneReport(
        lane="tests",
        passed=passed,
        commands_run=[{"command": "pytest", "exit_code": 0 if passed else 1}],
        failures=failures or [],
        summary="The agent says all green.",
    )


# ------------------------------------------------------------------ parsing


def test_a_junit_report_is_counted_from_its_test_cases(tmp_path: Path) -> None:
    write_reports(tmp_path, junit_xml=PYTEST_XML)
    evidence = read_evidence(tmp_path)
    assert (evidence.tests, evidence.passed, evidence.failed, evidence.errors, evidence.skipped) == (4, 2, 1, 0, 1)
    assert evidence.problem == "" and evidence.coverage_percent is None
    assert evidence.failures == ["tests.test_core::test_total: assert 3 == 4"]


def test_reports_from_several_files_and_suites_are_added_up(tmp_path: Path) -> None:
    write_reports(tmp_path, junit_xml=VITEST_XML, extra_xml=PYTEST_XML)
    evidence = read_evidence(tmp_path)
    assert evidence.tests == 6 and evidence.passed == 4 and evidence.failed == 1


def test_coverage_comes_from_cobertura_xml_or_coverage_json(tmp_path: Path) -> None:
    write_reports(tmp_path, junit_xml=VITEST_XML, coverage_xml=COBERTURA_XML)
    assert read_evidence(tmp_path).coverage_percent == pytest.approx(87.31)
    other = tmp_path / "other"
    write_reports(other, junit_xml=VITEST_XML, coverage_json='{"totals": {"percent_covered": 91.5}}')
    assert read_evidence(other).coverage_percent == pytest.approx(91.5)


def test_an_unreadable_coverage_file_is_ignored_not_fatal(tmp_path: Path) -> None:
    write_reports(tmp_path, junit_xml=VITEST_XML, coverage_json="not json")
    evidence = read_evidence(tmp_path)
    assert evidence.problem == "" and evidence.coverage_percent is None


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (None, "no JUnit report"),  # the folder holds nothing
        ("<testsuites><testsuite", "not valid XML"),
        ("<html><body>hi</body></html>", "no JUnit report"),  # not a JUnit root
        ('<!DOCTYPE x [<!ENTITY a "aaaa">]><testsuites>&a;</testsuites>', "DOCTYPE"),
    ],
)
def test_a_missing_or_unusable_report_is_a_problem_with_a_reason(tmp_path: Path, content: str | None, reason: str) -> None:
    write_reports(tmp_path, **({} if content is None else {"junit_xml": content}))
    problem = read_evidence(tmp_path).problem
    assert reason in problem


def test_no_reports_folder_at_all_is_a_problem(tmp_path: Path) -> None:
    assert "no JUnit report" in read_evidence(tmp_path).problem


def test_a_huge_report_is_refused_instead_of_parsed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("idea_to_mvp.implementation.reports.MAX_REPORT_BYTES", 100)
    write_reports(tmp_path, junit_xml=PYTEST_XML)
    assert "too large" in read_evidence(tmp_path).problem


def test_clearing_leaves_an_empty_folder_for_the_next_run(tmp_path: Path) -> None:
    write_reports(tmp_path, junit_xml=PYTEST_XML)
    clear_reports(tmp_path)
    assert (tmp_path / REPORTS_DIR).is_dir() and not list((tmp_path / REPORTS_DIR).iterdir())


# --------------------------------------------------------------- the verdict


def test_a_clean_report_adds_no_failures() -> None:
    assert evidence_failures(ReportedTests(tests=3, passed=3)) == []


def test_failures_and_errors_name_the_tests_that_failed() -> None:
    reasons = evidence_failures(ReportedTests(tests=3, passed=1, failed=1, errors=1, failures=["a::x: boom", "b::y: oops"]))
    assert len(reasons) == 1 and "2 test(s) failed or errored" in reasons[0] and "a::x: boom" in reasons[0]


def test_zero_collected_tests_is_a_failure() -> None:
    (reason,) = evidence_failures(ReportedTests())
    assert "no tests" in reason


def test_only_skipped_tests_is_a_failure_too() -> None:
    (reason,) = evidence_failures(ReportedTests(tests=2, skipped=2))
    assert "no test ran" in reason and "2 skipped" in reason


def test_a_problem_with_the_report_is_the_failure() -> None:
    assert evidence_failures(ReportedTests(problem="no JUnit report was found")) == ["no JUnit report was found"]


def test_the_agents_pass_is_overridden_by_a_failing_report() -> None:
    result = apply_evidence(lane(True), ReportedTests(tests=3, passed=2, failed=1, failures=["t::x: boom"]))
    assert result.passed is False and "t::x: boom" in result.failures[0]
    assert result.evidence is not None and result.evidence["failed"] == 1


def test_the_agents_pass_is_overridden_by_a_missing_report() -> None:
    result = apply_evidence(lane(True), ReportedTests(problem="no JUnit report was found in .idea-to-mvp/reports"))
    assert result.passed is False and "no JUnit report" in result.failures[0]


def test_a_clean_report_keeps_the_lanes_pass_and_records_the_counts() -> None:
    result = apply_evidence(lane(True), ReportedTests(tests=5, passed=4, skipped=1, coverage_percent=88.0))
    assert result.passed is True and result.failures == []
    assert result.evidence == ReportedTests(tests=5, passed=4, skipped=1, coverage_percent=88.0).model_dump()


def test_a_clean_report_does_not_rescue_a_lane_that_reported_failures() -> None:
    result = apply_evidence(lane(False, ["pip install failed"]), ReportedTests(tests=2, passed=2))
    assert result.passed is False and result.failures == ["pip install failed"]

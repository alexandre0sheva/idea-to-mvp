"""Deterministic test evidence: what the tests lane's test command left in `.idea-to-mvp/reports/`.

The tests lane is an agent, and `passed` in its report is its claim. When the plan declares a
`commands.test_report` (the test command written to also leave JUnit XML, and coverage when the stack has
it), the orchestrator reads those files itself and lets them overrule the claim: a missing or unreadable
report, zero tests that ran, or any failure or error fails the lane with the parsed reason. Reading a file
never runs agent code.

The files are written by the project's own test command, so code that command runs could write them too.
That raises the bar from "the agent said so" to "the test run said so"; it does not make a hostile project
trustworthy (see SECURITY.md). A report must come from this round: the folder is emptied before the lane
starts, so a green file left by an earlier round proves nothing.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple
from xml.etree import ElementTree

from pydantic import BaseModel

from idea_to_mvp.plan import REPORTS_DIR
from idea_to_mvp.text_utils import clip

if TYPE_CHECKING:  # pragma: no cover - typing only
    from idea_to_mvp.implementation.verify import LaneReport

LOGGER = logging.getLogger(__name__)

__all__ = [
    "REPORTS_DIR",
    "CaseResult",
    "ReportedTests",
    "apply_evidence",
    "clear_reports",
    "evidence_failures",
    "read_evidence",
]

MAX_REPORT_BYTES = 20_000_000  # a larger file is refused instead of parsed
MAX_REPORT_FILES = 50
_LISTED_FAILURES = 10
_MESSAGE_LIMIT = 200
_JUNIT_ROOTS = {"testsuites", "testsuite"}
MAX_CASES = 2000


class ReportedTests(BaseModel):
    """What the reports say about a test run (not a schema an agent fills in, so defaults are fine)."""

    tests: int = 0  # every test case listed, skipped ones included
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    coverage_percent: float | None = None
    failures: list[str] = []  # "<suite>::<test>: <message>" for the first failed tests
    problem: str = ""  # why no usable report was found; "" when one was read


def clear_reports(workspace: Path) -> Path:
    """An empty reports folder, so that whatever is found afterwards was written by the run that follows."""
    folder = Path(workspace) / REPORTS_DIR
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _root_of(path: Path) -> ElementTree.Element | str:
    """The XML root of a report file, or why it cannot be used."""
    try:
        size = path.stat().st_size
        if size > MAX_REPORT_BYTES:
            return f"{path.name} is too large to read ({size:,} bytes)"
        raw = path.read_bytes()
    except OSError as error:
        return f"{path.name} could not be read ({error.strerror or error})"
    head = raw[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in raw.lower():
        return f"{path.name} contains a DOCTYPE or entity declaration, which is not read"
    try:
        return ElementTree.fromstring(raw)
    except ElementTree.ParseError as error:
        return f"{path.name} is not valid XML ({error})"


def _case_name(case: ElementTree.Element) -> str:
    suite, name = case.get("classname") or "", case.get("name") or "?"
    return f"{suite}::{name}" if suite else name


def _case_result(case: ElementTree.Element) -> CaseResult:
    failure, error = case.find("failure"), case.find("error")
    node = failure if failure is not None else error
    if node is not None:
        detail = node.get("message") or node.get("type") or "failed"  # never the body: a traceback quotes the test
        return CaseResult(_case_name(case), "failed", clip(detail, _MESSAGE_LIMIT))
    return CaseResult(_case_name(case), "skipped" if case.find("skipped") is not None else "passed", "")


def _count_junit(root: ElementTree.Element, into: ReportedTests, cases: list[CaseResult] | None = None) -> None:
    for case in root.iter("testcase"):
        if cases is not None and len(cases) < MAX_CASES:
            cases.append(_case_result(case))
        into.tests += 1
        failure, error = case.find("failure"), case.find("error")
        node = failure if failure is not None else error
        if node is not None:
            if failure is not None:
                into.failed += 1
            else:
                into.errors += 1
            if len(into.failures) < _LISTED_FAILURES:
                detail = node.get("message") or node.text or node.get("type") or "failed"
                into.failures.append(f"{_case_name(case)}: {clip(detail, _MESSAGE_LIMIT)}")
        elif case.find("skipped") is not None:
            into.skipped += 1
        else:
            into.passed += 1


class CaseResult(NamedTuple):
    """One test case of a JUnit report. `message` is the failure's `message` attribute (its type when there is
    none), never the failure body: the body is a traceback, which quotes the test's code."""

    name: str  # "<classname>::<name>"
    status: Literal["passed", "failed", "skipped"]  # an error counts as failed
    message: str


def _cobertura_percent(root: ElementTree.Element) -> float | None:
    try:
        return round(float(root.attrib["line-rate"]) * 100, 2)
    except (KeyError, ValueError):
        return None


def _coverage_json_percent(path: Path) -> float | None:
    try:
        if path.stat().st_size > MAX_REPORT_BYTES:
            return None
        totals = json.loads(path.read_text(encoding="utf-8")).get("totals") or {}
        return round(float(totals["percent_covered"]), 2)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def read_evidence(workspace: Path, *, cases: list[CaseResult] | None = None) -> ReportedTests:
    """Parse every report in the workspace's reports folder: JUnit XML files are added up, a Cobertura
    `coverage.xml` or a coverage.py `coverage.json` gives the coverage. With `cases`, each test case is also
    appended to it (at most MAX_CASES), from the same single pass. Never raises."""
    folder = Path(workspace) / REPORTS_DIR
    evidence = ReportedTests()
    junit_files = 0
    problems: list[str] = []
    files = sorted(folder.glob("*.xml"))[:MAX_REPORT_FILES] if folder.is_dir() else []
    for path in files:
        root = _root_of(path)
        if isinstance(root, str):
            problems.append(root)
        elif root.tag in _JUNIT_ROOTS:
            _count_junit(root, evidence, cases)
            junit_files += 1
        elif root.tag == "coverage":
            evidence.coverage_percent = _cobertura_percent(root)
    if evidence.coverage_percent is None and (folder / "coverage.json").is_file():
        evidence.coverage_percent = _coverage_json_percent(folder / "coverage.json")
    if not junit_files:
        detail = f" ({'; '.join(problems)})" if problems else ""
        evidence.problem = f"no JUnit report was found in {REPORTS_DIR}/{detail}"
    return evidence


def evidence_failures(evidence: ReportedTests) -> list[str]:
    """Why the reports do not show a passing run (an empty list when they do)."""
    if evidence.problem:
        return [evidence.problem]
    broken = evidence.failed + evidence.errors
    if broken:
        listed = "; ".join(evidence.failures)
        more = f" (and {broken - len(evidence.failures)} more)" if broken > len(evidence.failures) else ""
        return [f"The test report shows {broken} test(s) failed or errored out of {evidence.tests}: {listed}{more}"]
    if evidence.tests == 0:
        return ["The test report lists no tests: the suite collected nothing."]
    if evidence.passed == 0:
        return [f"The test report shows no test ran ({evidence.skipped} skipped)."]
    return []


def apply_evidence(report: LaneReport, evidence: ReportedTests) -> LaneReport:
    """The tests lane's report with the evidence attached; the evidence can fail a lane but never rescue one."""
    reasons = evidence_failures(evidence)
    update: dict[str, object] = {"evidence": evidence.model_dump()}
    if reasons:
        update["passed"] = False
        update["failures"] = [*report.failures, *reasons]
    return report.model_copy(update=update)

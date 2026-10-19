"""Verification lanes: read-mostly agent sessions that each end in a structured `LaneReport`.

- **tests**: install dependencies and run the project's test suite (may write coverage artefacts).
- **quality**: lint / type checks, obvious security problems, and a smoke probe of the documented run command.
- **requirements**: map every P0 requirement of the PRD to at least one test and report the uncovered ones.
- **acceptance** (when the version has a held-out suite, `implementation/acceptance.py`): run the acceptance
  tests the implementers never saw in a throwaway copy, judged by their JUnit report alone.

The lanes run concurrently as `Send` branches of the graph (`nodes/verify.py`). A lane's answer is validated
against `LaneReport` (the SDK is asked for JSON of that schema); a reply that is missing or invalid fails
its lane *with the reason*, never silently. The overall verdict needs every lane to pass. The merged failure
list, not raw prose, is what the fix session receives.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypedDict

from pydantic import BaseModel, ValidationError
from pydantic.json_schema import SkipJsonSchema

from idea_to_mvp import implementer
from idea_to_mvp.implementation import acceptance as held_out
from idea_to_mvp.implementation.events import ImplEvent, events_from_sdk_message, make_event
from idea_to_mvp.implementation.integrity import IntegrityFinding, SessionWatch
from idea_to_mvp.implementation.ledger import SessionOutcome
from idea_to_mvp.implementation.options import SandboxUnavailableError
from idea_to_mvp.implementation.reports import (
    CaseResult,
    apply_evidence,
    clear_reports,
    read_evidence,
)
from idea_to_mvp.implementation.trajectory import SessionLog, model_label
from idea_to_mvp.plan import Plan, load_plan, parse_requirements
from idea_to_mvp.prompts_common import ANSWER_FREE, EVIDENCE_BOUNDARY, RUBRIC_ORDER, TEST_INTEGRITY
from idea_to_mvp.text_utils import read_text

if TYPE_CHECKING:  # pragma: no cover - typing only
    from idea_to_mvp.config import Settings
    from idea_to_mvp.model_catalog import AgentRole

LOGGER = logging.getLogger(__name__)

LaneName = Literal["tests", "quality", "requirements", "acceptance"]
LANES: tuple[LaneName, ...] = ("tests", "quality", "requirements")  # every round; "acceptance" only with a suite
ACCEPTANCE: LaneName = "acceptance"
WRITE_TOOLS = ("Edit", "MultiEdit", "Write")

_SUMMARY_LIMIT = 2000
_FAILURE_LIMIT = 400


class LaneReport(BaseModel):
    lane: LaneName
    passed: bool
    commands_run: list[dict[str, Any]]  # {"command": str, "exit_code": int}
    failures: list[str]
    summary: str
    # What the orchestrator read from the test reports (tests lane only). The agent is never asked for it
    # (it is left out of the schema) and `parse_lane_report` drops it if one sends it anyway.
    evidence: SkipJsonSchema[dict[str, Any] | None] = None


class Combined(TypedDict):
    """The lanes folded into one verdict (the node adds `attempts` to make a `VerificationResult`)."""

    passed: bool
    report: str
    lanes: list[dict[str, Any]]


@dataclass
class LaneOutcome:
    report: LaneReport
    cost_usd: float = 0.0
    turns: int = 0


@dataclass
class FixOutcome:
    success: bool
    summary: str
    cost_usd: float = 0.0
    turns: int = 0
    integrity: list[IntegrityFinding] = field(default_factory=list)  # what the session did to the tests


# ---------------------------------------------------------------------- prompts

_LANE_INTRO = (
    "You are the `{lane}` verification lane for this project. Other verification lanes check it at the same "
    "time. Do NOT fix anything and do not change the project's source; only inspect "
    "and report.\n"
)

_REPORT_FORMAT = """
## Your answer
Finish with a single JSON object and nothing else: {{"lane": "{lane}", "passed": <true|false>,
"commands_run": [{{"command": "<what you ran>", "exit_code": <int>}}], "failures": ["<one concrete problem
per entry, with the file or test and the relevant error>"], "summary": "<two or three sentences>"}}.
`passed` may be true only when `failures` is empty. If you could not do what this lane asks, that is a failure:
say why in `failures`.
"""

TESTS_PROMPT = """\
1. Read `README.md` and `quality/AGENTS.md` for the install and test commands.
{commands}2. Install the dependencies if they are not installed (you own the install step; the other lanes wait for it).
3. {run_tests}
4. Report every failing test with its error output. If the suite cannot run, or runs zero tests, that is a failure.
"""

_RUN_TESTS = (
    "Run the project's full test suite. Measure coverage if the project has coverage tooling; you may write\n"
    "   coverage artefacts, nothing else."
)
_RUN_TESTS_WITH_REPORT = (
    "Run the project's full test suite with the plan's test report command `{command}`, not the plain test\n"
    "   command. It leaves the machine-readable report the orchestrator reads from `.idea-to-mvp/reports/`, and the\n"
    "   orchestrator judges the run by that report. Never create, edit, or copy a report by hand. Measure\n"
    "   coverage if the project has coverage tooling (a coverage report may go in the same folder)."
)

QUALITY_PROMPT = """\
1. Read `README.md` and `quality/AGENTS.md` for the lint, type-check, and run commands.
{commands}2. Run the linter and the type checker where the project configures them. Do not run the install
   command: the tests lane installs dependencies at the same time. If a tool you need is not installed
   yet, wait a little and retry; if it is still missing, report that as a failure.
3. Look for obvious security problems: hard-coded secrets, unvalidated input reaching a shell, a query or a
   file path, `eval`, disabled certificate checks, debug modes enabled by default.
4. Smoke probe: if `README.md` documents a run command, start the project with it, check that it starts and
   answers one probe (an HTTP request, the first line of output, or `--help`), then stop it. A project that
   documents no run command (a library), or that cannot be probed without credentials, is skipped: say so
   in the summary; that alone is not a failure.
"""

REQUIREMENTS_PROMPT = """\
The PRD's requirements to cover:
{requirements}

For every one of them, find at least one automated test that exercises it (check test names, docstrings, and
what the assertions cover, not only the requirement id in a comment). A requirement no test really exercises
is uncovered. `failures` has one entry per uncovered requirement, starting with its id and priority, for
example `R2 (P0): no test covers exporting to CSV`. List the failures most serious first, by the order
below. You need not run the tests; read them.

""" + RUBRIC_ORDER


def _commands(plan: Plan | None) -> str:
    if plan is None:
        return ""
    lines = [
        f"   - {name}: `{value}`"
        for name, value in (
            ("install", plan.commands.install),
            ("test", plan.commands.test),
            ("lint", plan.commands.lint),
            ("run", plan.commands.run),
        )
        if value
    ]
    return "   The plan's commands:\n" + "\n".join(lines) + "\n" if lines else ""


def priority_requirements(prd_markdown: str) -> dict[str, str]:
    """The requirements a project must cover: the P0 ones, or all of them if none is marked P0."""
    requirements = parse_requirements(prd_markdown)
    p0 = {rid: priority for rid, priority in requirements.items() if priority == "P0"}
    return p0 or requirements


def build_lane_prompt(lane: LaneName, plan: Plan | None, prd_markdown: str) -> str:
    if lane == "tests":
        report_command = (plan.commands.test_report or "").strip() if plan is not None else ""
        run_tests = _RUN_TESTS_WITH_REPORT.format(command=report_command) if report_command else _RUN_TESTS
        body = TESTS_PROMPT.format(commands=_commands(plan), run_tests=run_tests)
    elif lane == "quality":
        body = QUALITY_PROMPT.format(commands=_commands(plan))
    else:
        required = priority_requirements(prd_markdown)
        listing = "\n".join(f"- {rid} ({priority or 'unprioritised'})" for rid, priority in required.items())
        body = REQUIREMENTS_PROMPT.format(
            requirements=listing or "- (none could be parsed from PRD.md: read it and cover every requirement it states)"
        )
    return _LANE_INTRO.format(lane=lane) + "\n" + body + "\n" + EVIDENCE_BOUNDARY + _REPORT_FORMAT.format(lane=lane)


FIX_PROMPT = (
    "You are the implementation agent for this project. Verification just found these failures:\n\n"
    "{failures}\n\n"
    "Diagnose and fix them, keeping changes minimal and consistent with `ARCHITECTURE.md` and the contracts in "
    "`plan.md`. A failure tagged `[requirements]` means a requirement has no test: write the missing test. "
    "A failure tagged `[integrity]` means an earlier session deleted, renamed, skipped, or weakened a test: "
    "restore the test, or, only if the change was right, justify it in `quality/TEST_CHANGES.md` as the failure "
    "says. Re-run the affected checks to confirm the fix. Do not run `git commit`; the orchestrator commits. "
    "Finish with a short summary of what you changed.\n\n"
    + ANSWER_FREE
    + "\n"
    + TEST_INTEGRITY
    + "\n"
    + EVIDENCE_BOUNDARY
)


# ---------------------------------------------------------------------- reports


def _invalid(lane: str, reason: str, text: str = "") -> LaneReport:
    return LaneReport(
        lane=lane,  # type: ignore[arg-type]
        passed=False,
        commands_run=[],
        failures=[f"The {lane} lane did not return a valid report: {reason}"],
        summary=(text or "").strip()[:_SUMMARY_LIMIT] or "The lane produced no report.",
    )


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _json_in(text: str) -> Any:
    """The JSON object in an agent's reply (bare, fenced, or embedded in prose); raises ValueError if none."""
    text = (text or "").strip()
    candidates = [text, *(match.strip() for match in _FENCE.findall(text))]
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    raise ValueError("the reply is not JSON")


def parse_lane_report(lane: str, structured: Any, text: str) -> LaneReport:
    """Validate a lane's answer (SDK structured output, else JSON in the reply). Never raises: anything
    unusable becomes a failing report that says why."""
    data = structured
    if data is None:
        if not (text or "").strip():
            return _invalid(lane, "no report at all (the reply was empty).")
        try:
            data = _json_in(text)
        except ValueError:
            return _invalid(lane, "the reply is not the JSON report that was asked for.", text)
    try:
        parsed = LaneReport.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "report"
        return _invalid(lane, f"{where}: {first['msg']}.", text)
    if parsed.lane != lane:
        return _invalid(lane, f"it is a report for the {parsed.lane} lane.", text)
    parsed = parsed.model_copy(update={"evidence": None})  # evidence comes from the orchestrator, never the agent
    if parsed.passed and parsed.failures:
        return parsed.model_copy(update={"passed": False})  # a lane that lists failures has not passed
    return parsed


def _missing(lane: str) -> LaneReport:
    return LaneReport(
        lane=lane,  # type: ignore[arg-type]
        passed=False,
        commands_run=[],
        failures=[f"The {lane} lane did not report."],
        summary="No report was received from this lane.",
    )


def combine_lanes(reports: Sequence[LaneReport], lanes: Sequence[LaneName] = LANES) -> Combined:
    """One verdict from the lanes: passed only if every expected lane reported and passed (a report of a lane
    that was not expected this round, such as an acceptance lane left over from an earlier version, is ignored)."""
    by_lane = {report.lane: report for report in reports}
    ordered = [by_lane.get(lane) or _missing(lane) for lane in lanes]
    blocks = []
    for lane_report in ordered:
        lines = [f"### {lane_report.lane.capitalize()} lane: {'PASSED' if lane_report.passed else 'FAILED'}", "", lane_report.summary.strip()]
        commands = [f"`{run.get('command', '?')}` (exit {run.get('exit_code', '?')})" for run in lane_report.commands_run]
        if commands:
            lines += ["", "Commands: " + ", ".join(commands)]
        if lane_report.failures:
            lines += ["", "Failures:", *(f"- {failure}" for failure in lane_report.failures)]
        blocks.append("\n".join(lines))
    return {
        "passed": all(lane_report.passed for lane_report in ordered),
        "report": "\n\n".join(blocks),
        "lanes": [lane_report.model_dump() for lane_report in ordered],
    }


def failure_list(lanes: Sequence[dict[str, Any]]) -> list[str]:
    """The failures of every failed lane, merged into one list, each tagged with its lane."""
    failures: list[str] = []
    for lane in lanes:
        if lane.get("passed"):
            continue
        entries = [str(failure) for failure in lane.get("failures") or []]
        if not entries:
            entries = [f"the lane failed: {str(lane.get('summary') or 'no details').strip()}"]
        failures += [f"[{lane.get('lane')}] {entry[:_FAILURE_LIMIT]}" for entry in entries]
    return failures


# --------------------------------------------------------------------- sessions


@dataclass
class _Session:
    structured: Any = None
    text: str = ""
    is_error: bool = False
    subtype: str = ""
    cost_usd: float = 0.0
    turns: int = 0
    crashed: str = ""  # why the session raised, if it did
    session_id: str | None = None


async def _run_session(
    workspace: Path,
    settings: Settings,
    *,
    prompt: str,
    task_id: str,
    max_turns: int,
    budget_usd: float,
    emit: Callable[[ImplEvent], None],
    output_schema: dict[str, Any] | None = None,
    extra_disallowed_tools: Sequence[str] = (),
    role: AgentRole,
    query_fn: Callable[..., AsyncIterator[Any]] | None = None,
    log: SessionLog | None = None,
) -> _Session:
    from claude_agent_sdk import ResultMessage

    session = _Session()
    try:
        async for message in implementer.iter_agent_messages(
            prompt,
            workspace=workspace,
            settings=settings,
            max_turns=max_turns,
            max_budget_usd=budget_usd,
            output_schema=output_schema,
            extra_disallowed_tools=extra_disallowed_tools,
            role=role,
            query_fn=query_fn,
        ):
            if log is not None:
                log.message(message)
            for event in events_from_sdk_message(message, task_id):
                emit(event)
            if isinstance(message, ResultMessage):
                session.structured = message.structured_output
                session.text = (message.result or "").strip()
                session.is_error = bool(message.is_error)
                session.subtype = message.subtype
                session.cost_usd = float(message.total_cost_usd or 0.0)
                session.turns = message.num_turns
                session.session_id = message.session_id
    except SandboxUnavailableError:
        raise  # a configuration problem, not a failed check
    except Exception as exc:  # the agent process died, the API failed, ...
        LOGGER.exception("Verification session %s failed", task_id)
        session.crashed = f"{type(exc).__name__}: {exc}"[:_SUMMARY_LIMIT]
    return session


_read = read_text


def _finish_log(log: SessionLog, session: _Session) -> None:
    """Close a session's trajectory and add it to the ledger (a lane that fails its checks is still a session that worked)."""
    outcome: SessionOutcome = "crashed" if session.crashed else "failed" if session.is_error else "success"
    note = session.crashed or (f"The session ended with {session.subtype}. {session.text}".strip() if session.is_error else "")
    log.finish(outcome=outcome, cost_usd=session.cost_usd, turns=session.turns, session_id=session.session_id, note=note)


def log_name(iteration: int, name: str) -> str:
    """Trajectory label of a verification session; later versions are prefixed so their files stay apart."""
    return f"v{iteration}-{name}" if iteration > 1 else name


_UNBACKED = " (Not backed by a test report: the plan declares no test report command, so this is the agent's word.)"


def check_test_report(workspace: Path, plan: Plan | None, report: LaneReport) -> LaneReport:
    """The tests lane's report after the orchestrator has read the test reports.

    A plan with a `test_report` command is judged by the files that command left (they can fail the lane, never
    rescue it). A plan from before the field existed keeps the agent's verdict, with a warning and a note in the
    summary. No plan at all is left alone.
    """
    if plan is None:
        return report
    if (plan.commands.test_report or "").strip():
        return apply_evidence(report, read_evidence(workspace))
    LOGGER.warning("The plan has no test report command, so the tests lane's verdict is the agent's claim.")
    return report.model_copy(update={"summary": report.summary.rstrip() + _UNBACKED})


def _lane_report_from(lane: LaneName, session: _Session) -> LaneReport:
    if session.crashed:
        return _invalid(lane, f"the session failed ({session.crashed}).")
    if session.is_error:
        return _invalid(lane, f"the session ended with an error ({session.subtype}).", session.text)
    return parse_lane_report(lane, session.structured, session.text)


async def run_lane(
    workspace: Path,
    lane: LaneName,
    settings: Settings,
    *,
    budget_usd: float,
    emit: Callable[[ImplEvent], None],
    plan: Plan | None = None,
    iteration: int = 1,
    attempt: int = 1,
    acceptance_dir: Path | None = None,
    query_fn: Callable[..., AsyncIterator[Any]] | None = None,
) -> LaneOutcome:
    """Run one verification lane in `workspace` and return its validated report (never raises for a bad lane).

    `attempt` is the verification round (1 = before any fix) and `iteration` the version being built; they
    name the lane's trajectory and ledger row. The acceptance lane runs the suite saved in `acceptance_dir`.
    """
    workspace = Path(workspace)
    if lane == ACCEPTANCE:
        return await _run_acceptance(
            workspace, settings, budget_usd=budget_usd, emit=emit, folder=acceptance_dir, plan=plan,
            iteration=iteration, attempt=attempt, query_fn=query_fn,
        )  # fmt: skip
    task_id, label = f"verify:{lane}", f"{lane.capitalize()} lane"
    emit(make_event("task_start", task_id=task_id, label=label))
    log = SessionLog(
        workspace,
        kind="lane",
        label=log_name(iteration, f"lane-{lane}-{attempt}"),
        iteration=iteration,
        attempt=attempt,
        model=model_label(settings, "verifier"),
    )
    plan = plan or load_plan(_read(workspace / "plan.json"))
    if lane == "tests":
        clear_reports(workspace)  # whatever is in the folder afterwards was written by this round's run
    if settings.demo_mode:
        from idea_to_mvp.demo.implementer import demo_verify_lane

        await asyncio.sleep(0)
        outcome = LaneOutcome(await asyncio.to_thread(demo_verify_lane, workspace, lane))
        log.event(make_event("text", task_id=task_id, label="agent", detail=outcome.report.summary))
        log.finish(outcome="success", cost_usd=0.0, turns=0, session_id=None)
    else:
        session = await _run_session(
            workspace,
            settings,
            prompt=build_lane_prompt(lane, plan, _read(workspace / "PRD.md")),
            task_id=task_id,
            max_turns=settings.verifier_max_turns,
            budget_usd=budget_usd,
            emit=emit,
            output_schema=LaneReport.model_json_schema(),
            extra_disallowed_tools=() if lane == "tests" else WRITE_TOOLS,
            role="verifier",
            query_fn=query_fn,
            log=log,
        )
        _finish_log(log, session)
        outcome = LaneOutcome(_lane_report_from(lane, session), session.cost_usd, session.turns)
    if lane == "tests":
        outcome.report = check_test_report(workspace, plan, outcome.report)
    detail = "passed" if outcome.report.passed else f"failed: {outcome.report.failures[0][:120] if outcome.report.failures else 'see report'}"
    emit(make_event("task_end", task_id=task_id, label=label, detail=detail))
    return outcome


def build_acceptance_prompt(plan: Plan | None, command: str) -> str:
    install = (plan.commands.install or "").strip() if plan is not None else ""
    steps = held_out.LANE_STEPS.format(
        install=f" with `{install}`" if install else " if the project has any",
        command=command,
        report=held_out.ACCEPTANCE_REPORT,
    )
    return _LANE_INTRO.format(lane=ACCEPTANCE) + "\n" + steps + "\n" + EVIDENCE_BOUNDARY + _REPORT_FORMAT.format(lane=ACCEPTANCE)


def _acceptance_report(failures: list[str], summary: str, commands: list[dict[str, Any]] | None = None, evidence: dict[str, Any] | None = None) -> LaneReport:
    return LaneReport(
        lane=ACCEPTANCE, passed=not failures, commands_run=commands or [], failures=failures, summary=summary, evidence=evidence
    )


async def _run_acceptance(
    workspace: Path,
    settings: Settings,
    *,
    budget_usd: float,
    emit: Callable[[ImplEvent], None],
    folder: Path | None,
    plan: Plan | None,
    iteration: int,
    attempt: int,
    query_fn: Callable[..., AsyncIterator[Any]] | None,
) -> LaneOutcome:
    """The acceptance lane: the held-out suite run in a throwaway copy, judged by its JUnit report alone.

    Nothing the session says reaches the report (it has read the tests, and its words would carry them to a fix
    session): the failures are the failing tests' names and assertion messages, the summary is the
    orchestrator's. Its trajectory is kept beside the suite, outside the workspace.
    """
    task_id, label = "verify:acceptance", "Acceptance lane"
    emit(make_event("task_start", task_id=task_id, label=label))
    suite = held_out.load_suite(folder)
    if folder is None or suite is None or not suite.files:
        report = _acceptance_report(["No acceptance tests were written for this version, so none could run."], "No held-out suite.")
        emit(make_event("task_end", task_id=task_id, label=label, detail="failed: no acceptance tests"))
        return LaneOutcome(report)
    plan = plan or load_plan(_read(workspace / "plan.json"))
    log = SessionLog(
        workspace,
        kind="lane",
        label=log_name(iteration, f"lane-{ACCEPTANCE}-{attempt}"),
        iteration=iteration,
        attempt=attempt,
        model=model_label(settings, "verifier"),
        trajectory_dir=Path(folder).parent / held_out.TRAJECTORY_DIR,
    )
    try:
        scratch = await asyncio.to_thread(held_out.make_scratch, workspace, Path(folder), f"v{iteration}-{attempt}")
    except (OSError, shutil.Error) as error:  # a full disk, a file that cannot be read: a failed lane, not a crash
        LOGGER.warning("Could not copy %s for the acceptance lane: %s", workspace, error)
        reason = f"The acceptance lane could not copy the project to run the suite ({type(error).__name__})."
        report = _acceptance_report([reason], "The held-out suite could not run.")
        emit(make_event("task_end", task_id=task_id, label=label, detail="failed: could not copy the project"))
        return LaneOutcome(report)
    outcome = LaneOutcome(_acceptance_report([], ""))
    notes: list[str] = []
    try:
        if settings.demo_mode:
            from idea_to_mvp.demo.implementer import demo_run_acceptance

            code = await asyncio.to_thread(demo_run_acceptance, scratch, suite.command)
            commands = [{"command": suite.command, "exit_code": code}]
            log.event(make_event("text", task_id=task_id, label="agent", detail=f"Ran the acceptance suite (exit {code})."))
            log.finish(outcome="success", cost_usd=0.0, turns=0, session_id=None)
        else:
            session = await _run_session(
                scratch,
                settings,
                prompt=build_acceptance_prompt(plan, suite.command),
                task_id=task_id,
                max_turns=settings.verifier_max_turns,
                budget_usd=budget_usd,
                emit=emit,
                output_schema=LaneReport.model_json_schema(),
                extra_disallowed_tools=WRITE_TOOLS,
                role="verifier",
                query_fn=query_fn,
                log=log,
            )
            _finish_log(log, session)
            outcome.cost_usd, outcome.turns = session.cost_usd, session.turns
            commands = parse_lane_report(ACCEPTANCE, session.structured, session.text).commands_run if not session.crashed else []
            if session.crashed or session.is_error:
                notes.append("The acceptance session did not finish normally.")
        cases: list[CaseResult] = []
        evidence = read_evidence(scratch, cases=cases)  # one pass gives the counts and the cases
    finally:
        await asyncio.to_thread(held_out.remove_scratch, workspace, scratch)
    failures = held_out.acceptance_failures(evidence, cases)
    requirements = held_out.requirement_status(cases)
    passed_requirements = sum(1 for status in requirements.values() if status == "passed")
    summary = (
        f"{evidence.passed} of {evidence.tests} held-out acceptance test(s) passed; "
        f"{passed_requirements} of {len(requirements)} requirement(s) they check passed."
    )
    recorded = {**evidence.model_dump(), "failures": failures, "requirements": requirements, "suite": str(folder)}
    outcome.report = _acceptance_report([*notes, *failures] if failures else [], summary, commands, recorded)
    detail = "passed" if outcome.report.passed else f"failed: {outcome.report.failures[0][:120]}"
    emit(make_event("task_end", task_id=task_id, label=label, detail=detail))
    return outcome


def build_fix_prompt(failures: list[str]) -> str:
    """The fix prompt; failures from the held-out acceptance tests add a note on what they are (only then, so a
    fix session without them is not told that such tests exist)."""
    prompt = FIX_PROMPT.format(failures="\n".join(f"- {failure}" for failure in failures) or "- (none listed)")
    if any(failure.startswith(f"[{ACCEPTANCE}]") for failure in failures):
        prompt += "\n" + held_out.FIX_NOTE
    return prompt


def fix_role(attempt: int) -> AgentRole:
    """The kind of session a fix attempt (1, 2, ...) runs as."""
    return "implementer" if attempt <= 1 else "escalation"


async def run_fix(
    workspace: Path,
    failures: list[str],
    settings: Settings,
    *,
    budget_usd: float,
    emit: Callable[[ImplEvent], None],
    attempt: int = 1,
    iteration: int = 1,
    query_fn: Callable[..., AsyncIterator[Any]] | None = None,
) -> FixOutcome:
    """One implementation session that fixes the merged failure list (a failing session is reported, not raised).

    The first attempt runs on the task sessions' model; later ones escalate, since that model already failed once.
    The test files are compared before and after the session (`integrity.SessionWatch`), as for task sessions.
    """
    task_id, label = "verify:fix", "Fix"
    emit(make_event("task_start", task_id=task_id, label=label, detail=f"{len(failures)} failure(s)"))
    log = SessionLog(
        workspace,
        kind="fix",
        label=log_name(iteration, f"fix-{attempt}"),
        iteration=iteration,
        attempt=attempt,
        model=model_label(settings, fix_role(attempt)),
    )
    watch = await asyncio.to_thread(
        SessionWatch, Path(workspace), settings, session=log_name(iteration, f"fix-{attempt}"), iteration=iteration
    )
    if settings.demo_mode:
        from idea_to_mvp.demo.implementer import demo_fix

        await asyncio.sleep(0)
        outcome = FixOutcome(True, demo_fix("\n".join(failures)))
        log.event(make_event("text", task_id=task_id, label="agent", detail=outcome.summary))
        log.finish(outcome="success", cost_usd=0.0, turns=0, session_id=None)
    else:
        session = await _run_session(
            Path(workspace),
            settings,
            prompt=build_fix_prompt(failures),
            task_id=task_id,
            max_turns=settings.implementer_max_task_turns,
            budget_usd=budget_usd,
            emit=emit,
            role=fix_role(attempt),
            query_fn=query_fn,
            log=log,
        )
        _finish_log(log, session)
        if session.crashed:
            summary = f"The fix session failed: {session.crashed}"
        else:
            summary = session.text or f"The fix session ended with {session.subtype}."
        outcome = FixOutcome(
            success=not session.crashed and not session.is_error,
            summary=summary[:_SUMMARY_LIMIT],
            cost_usd=session.cost_usd,
            turns=session.turns,
        )
    outcome.integrity = await asyncio.to_thread(watch.findings, session=log.label)
    emit(make_event("task_end", task_id=task_id, label=label, detail="done" if outcome.success else f"failed: {outcome.summary.splitlines()[0][:120]}"))
    return outcome

"""Verification lanes: three read-mostly agent sessions that each end in a structured `LaneReport`.

- **tests**: install dependencies and run the project's test suite (may write coverage artefacts).
- **quality**: lint / type checks, obvious security problems, and a smoke probe of the documented run command.
- **requirements**: map every P0 requirement of the PRD to at least one test and report the uncovered ones.

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
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypedDict

from pydantic import BaseModel, ValidationError

from idea_to_mvp import implementer
from idea_to_mvp.implementation.events import ImplEvent, events_from_sdk_message, make_event
from idea_to_mvp.implementation.options import SandboxUnavailableError
from idea_to_mvp.plan import Plan, load_plan, parse_requirements

if TYPE_CHECKING:  # pragma: no cover - typing only
    from idea_to_mvp.config import Settings

LOGGER = logging.getLogger(__name__)

LaneName = Literal["tests", "quality", "requirements"]
LANES: tuple[LaneName, ...] = ("tests", "quality", "requirements")
WRITE_TOOLS = ("Edit", "MultiEdit", "Write")

_SUMMARY_LIMIT = 2000
_FAILURE_LIMIT = 400


class LaneReport(BaseModel):
    lane: LaneName
    passed: bool
    commands_run: list[dict[str, Any]]  # {"command": str, "exit_code": int}
    failures: list[str]
    summary: str


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


# ---------------------------------------------------------------------- prompts

_LANE_INTRO = (
    "You are the `{lane}` verification lane for this project. Two other lanes (tests, quality, requirements) "
    "verify it at the same time. Do NOT fix anything and do not change the project's source; only inspect "
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
3. Run the project's full test suite. Measure coverage if the project has coverage tooling; you may write
   coverage artefacts, nothing else.
4. Report every failing test with its error output. If the suite cannot run, or runs zero tests, that is a failure.
"""

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
example `R2 (P0): no test covers exporting to CSV`. You need not run the tests; read them.
"""


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
        body = TESTS_PROMPT.format(commands=_commands(plan))
    elif lane == "quality":
        body = QUALITY_PROMPT.format(commands=_commands(plan))
    else:
        required = priority_requirements(prd_markdown)
        listing = "\n".join(f"- {rid} ({priority or 'unprioritised'})" for rid, priority in required.items())
        body = REQUIREMENTS_PROMPT.format(
            requirements=listing or "- (none could be parsed from PRD.md: read it and cover every requirement it states)"
        )
    return _LANE_INTRO.format(lane=lane) + "\n" + body + _REPORT_FORMAT.format(lane=lane)


FIX_PROMPT = (
    "You are the implementation agent for this project. Verification just found these failures:\n\n"
    "{failures}\n\n"
    "Diagnose and fix them, keeping changes minimal and consistent with `ARCHITECTURE.md` and the contracts in "
    "`plan.md`. A failure tagged `[requirements]` means a requirement has no test: write the missing test. "
    "Re-run the affected checks to confirm the fix. Do not run `git commit`; the orchestrator commits. "
    "Finish with a short summary of what you changed."
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


def combine_lanes(reports: Sequence[LaneReport]) -> Combined:
    """One verdict from the lanes: passed only if all three lanes reported and passed."""
    by_lane = {report.lane: report for report in reports}
    ordered = [by_lane.get(lane) or _missing(lane) for lane in LANES]
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
    query_fn: Callable[..., AsyncIterator[Any]] | None = None,
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
            query_fn=query_fn,
        ):
            for event in events_from_sdk_message(message, task_id):
                emit(event)
            if isinstance(message, ResultMessage):
                session.structured = message.structured_output
                session.text = (message.result or "").strip()
                session.is_error = bool(message.is_error)
                session.subtype = message.subtype
                session.cost_usd = float(message.total_cost_usd or 0.0)
                session.turns = message.num_turns
    except SandboxUnavailableError:
        raise  # a configuration problem, not a failed check
    except Exception as exc:  # the agent process died, the API failed, ...
        LOGGER.exception("Verification session %s failed", task_id)
        session.crashed = f"{type(exc).__name__}: {exc}"[:_SUMMARY_LIMIT]
    return session


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


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
    query_fn: Callable[..., AsyncIterator[Any]] | None = None,
) -> LaneOutcome:
    """Run one verification lane in `workspace` and return its validated report (never raises for a bad lane)."""
    workspace = Path(workspace)
    task_id, label = f"verify:{lane}", f"{lane.capitalize()} lane"
    emit(make_event("task_start", task_id=task_id, label=label))
    if settings.demo_mode:
        from idea_to_mvp.demo.implementer import demo_verify_lane

        await asyncio.sleep(0)
        outcome = LaneOutcome(await asyncio.to_thread(demo_verify_lane, workspace, lane))
    else:
        plan = plan or load_plan(_read(workspace / "plan.json"))
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
            query_fn=query_fn,
        )
        outcome = LaneOutcome(_lane_report_from(lane, session), session.cost_usd, session.turns)
    detail = "passed" if outcome.report.passed else f"failed: {outcome.report.failures[0][:120] if outcome.report.failures else 'see report'}"
    emit(make_event("task_end", task_id=task_id, label=label, detail=detail))
    return outcome


async def run_fix(
    workspace: Path,
    failures: list[str],
    settings: Settings,
    *,
    budget_usd: float,
    emit: Callable[[ImplEvent], None],
    query_fn: Callable[..., AsyncIterator[Any]] | None = None,
) -> FixOutcome:
    """One implementation session that fixes the merged failure list (a failing session is reported, not raised)."""
    task_id, label = "verify:fix", "Fix"
    emit(make_event("task_start", task_id=task_id, label=label, detail=f"{len(failures)} failure(s)"))
    if settings.demo_mode:
        from idea_to_mvp.demo.implementer import demo_fix

        await asyncio.sleep(0)
        outcome = FixOutcome(True, demo_fix("\n".join(failures)))
    else:
        session = await _run_session(
            Path(workspace),
            settings,
            prompt=FIX_PROMPT.format(failures="\n".join(f"- {failure}" for failure in failures) or "- (none listed)"),
            task_id=task_id,
            max_turns=settings.implementer_max_task_turns,
            budget_usd=budget_usd,
            emit=emit,
            query_fn=query_fn,
        )
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
    emit(make_event("task_end", task_id=task_id, label=label, detail="done" if outcome.success else f"failed: {outcome.summary.splitlines()[0][:120]}"))
    return outcome

"""Verification as graph steps: `verify` fans out to the lanes, `verdict` folds them, `fix` repairs.

```
implement → verify ─Send×3|4→ verify_lane → verdict ─ pass / exhausted ─→ delivery_report
               ↑                              └──────── retry ──→ fix ─┘ (back to verify)
```

Each stage is its own node, so every fix attempt is a checkpoint and a stream event instead of a `while`
loop hidden inside one node. The loop ends when the lanes pass, when `MAX_FIX_ATTEMPTS` fixes were made,
or when the whole-run budget cannot pay for another round. Lanes and sessions are described in
`implementation/verify.py`; a version with a held-out acceptance suite (`nodes/acceptance.py`) gets a fourth lane.

The verdict also weighs test integrity (`implementation/integrity.py`): what the task, resolver, and fix
sessions did to the tests, plus a drop in the test count the test report lists. With `TEST_INTEGRITY=block`
an open finding fails the verdict and reaches the fix session as an `[integrity]` failure.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Literal, TypedDict

from langgraph.types import Send

from idea_to_mvp import implementer
from idea_to_mvp.config import Settings, get_settings
from idea_to_mvp.implementation import integrity
from idea_to_mvp.implementation import verify as lanes
from idea_to_mvp.implementation.integrity import IntegrityFinding
from idea_to_mvp.implementation.verify import LaneName, LaneReport
from idea_to_mvp.model_catalog import AgentRole
from idea_to_mvp.nodes.acceptance import lanes_for
from idea_to_mvp.nodes.implement import emitter
from idea_to_mvp.roles import resolve_agent_model
from idea_to_mvp.state import IdeaDiscussionState, VerificationResult
from idea_to_mvp.usage import UsageRecord, summarize_usage

LOGGER = logging.getLogger(__name__)

_MIN_BUDGET_USD = 0.01  # below this no session can usefully start


class LaneInput(TypedDict):
    """Payload of one `Send("verify_lane", ...)`."""

    lane: LaneName
    workspace_dir: str
    budget_usd: float
    iteration: int  # the version being verified
    attempt: int  # the verification round: 1 before any fix
    acceptance_dir: str  # the version's held-out suite (the acceptance lane only; '' for the others)


def remaining_budget(state: IdeaDiscussionState, settings: Settings) -> float:
    """What is left of the whole-run cap after everything this version has spent (implementation, lanes,
    fixes). Earlier versions of an iterated project are not counted: each has its own budget."""
    spent = summarize_usage(state.get("usage") or [])["agent_cost_usd"] - float(state.get("spent_before_iteration") or 0.0)
    return max(0.0, settings.implementer_max_total_usd - spent)


def _record(role: str, settings: Settings, cost_usd: float, kind: AgentRole) -> UsageRecord:
    return {
        "role": role,
        "provider": "anthropic",
        "model": resolve_agent_model(settings, kind),
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": cost_usd,
    }


def _previous(state: IdeaDiscussionState) -> VerificationResult:
    return state.get("verification") or {"passed": False, "attempts": 0, "report": "", "lanes": []}


# ------------------------------------------------------------------------ verify


def verify_node(state: IdeaDiscussionState) -> dict[str, Any]:
    """Start a verification round. Without budget for any session the round is skipped and says so."""
    settings = get_settings()
    if remaining_budget(state, settings) >= _MIN_BUDGET_USD:
        return {"stage": "verification"}
    LOGGER.warning("Whole-run budget used up; skipping verification.")
    previous = _previous(state)
    report = (
        f"Verification was not run: the whole-run budget (${settings.implementer_max_total_usd:.2f}, "
        "IMPLEMENTER_MAX_TOTAL_USD) is used up."
    )
    if previous.get("report"):
        report += f"\n\nThe last verification round found:\n\n{previous['report']}"
    if block := integrity.report_block(state.get("integrity_findings") or {}, settings.test_integrity):
        report += f"\n\n{block}"  # what sessions did to the tests is reported even when nothing was verified
    skipped: VerificationResult = {
        "passed": False,
        "attempts": int(previous.get("attempts") or 0),
        "report": report,
        "lanes": list(previous.get("lanes") or []),
        "integrity": settings.test_integrity,
    }
    return {"verification": skipped, "stage": "report"}


def route_after_verify(state: IdeaDiscussionState) -> list[Send] | Literal["delivery_report"]:
    settings = get_settings()
    remaining = remaining_budget(state, settings)
    if remaining < _MIN_BUDGET_USD:
        return "delivery_report"
    # The lanes run at once, so each may spend the per-task cap or its share of what is left.
    lanes = lanes_for(state)
    budget = min(settings.implementer_max_task_usd, remaining / len(lanes))
    workspace = str(state["workspace_dir"])
    iteration = int(state.get("iteration") or 1)
    attempt = int(_previous(state).get("attempts") or 0) + 1
    suite = str((state.get("acceptance") or {}).get("dir") or "")
    return [
        Send(
            "verify_lane",
            LaneInput(
                lane=lane,
                workspace_dir=workspace,
                budget_usd=budget,
                iteration=iteration,
                attempt=attempt,
                acceptance_dir=suite if lane == "acceptance" else "",
            ),
        )
        for lane in lanes
    ]


async def verify_lane_node(state: LaneInput) -> dict[str, Any]:
    settings = get_settings()
    outcome = await lanes.run_lane(
        Path(state["workspace_dir"]),
        state["lane"],
        settings,
        budget_usd=state["budget_usd"],
        emit=emitter(),
        iteration=state.get("iteration", 1),
        attempt=state.get("attempt", 1),
        acceptance_dir=Path(state["acceptance_dir"]) if state.get("acceptance_dir") else None,
    )
    update: dict[str, Any] = {"lane_reports": {state["lane"]: outcome.report.model_dump()}}
    if outcome.cost_usd > 0:
        update["usage"] = [_record("verifier", settings, outcome.cost_usd, "verifier")]
    return update


# ------------------------------------------------------------------------ verdict


def next_step(state: IdeaDiscussionState, settings: Settings) -> Literal["pass", "retry", "exhausted"]:
    verification = _previous(state)
    if verification.get("passed"):
        return "pass"
    if int(verification.get("attempts") or 0) >= settings.max_fix_attempts:
        return "exhausted"
    return "retry" if remaining_budget(state, settings) >= _MIN_BUDGET_USD else "exhausted"


def _reported_test_count(reports: list[LaneReport]) -> int | None:
    """How many tests this round's test report lists (None without a usable report)."""
    for report in reports:
        evidence = report.evidence or {}
        if report.lane == "tests" and evidence and not evidence.get("problem"):
            return int(evidence.get("tests") or 0)
    return None


def _check_integrity(
    state: IdeaDiscussionState, settings: Settings, reports: list[LaneReport], attempts: int
) -> tuple[dict[str, Any], dict[str, IntegrityFinding]]:
    """(state update, every finding as it now stands) after a verification round: a drop in the reported test
    count is a new finding, and open findings whose test is back or whose change is justified are closed."""
    if settings.test_integrity == "off":
        return {}, {}
    findings = dict(state.get("integrity_findings") or {})
    count = _reported_test_count(reports)
    iteration = int(state.get("iteration") or 1)
    workspace_dir = str(state.get("workspace_dir") or "")  # never '' (that would read the current directory)
    workspace = Path(workspace_dir) if workspace_dir and Path(workspace_dir).is_dir() else None
    changed: dict[str, IntegrityFinding] = {}
    if count is not None:
        # What TEST_CHANGES.md says about the count before this round cannot justify a drop seen in it.
        stale = integrity.read_justifications(workspace).get(integrity.COUNT_KEY, "") if workspace else ""
        session = lanes.log_name(iteration, f"lane-tests-{attempts + 1}")
        previous = state.get("integrity_test_count")
        if dropped := integrity.count_finding(previous, count, session=session, iteration=iteration, stale=stale):
            changed[dropped["id"]] = dropped
    findings.update(changed)
    reviewed = integrity.review(findings, workspace, test_count=count) if workspace else {}
    changed.update(reviewed)
    findings.update(reviewed)
    update: dict[str, Any] = {"integrity_findings": changed} if changed else {}
    if count is not None:
        update["integrity_test_count"] = count
    return update, findings


def verdict_node(state: IdeaDiscussionState) -> dict[str, Any]:
    settings = get_settings()
    reports = [LaneReport.model_validate(report) for report in (state.get("lane_reports") or {}).values()]
    combined = lanes.combine_lanes(reports, lanes_for(state))
    attempts = int(_previous(state).get("attempts") or 0)
    update, findings = _check_integrity(state, settings, reports, attempts)
    mode = settings.test_integrity
    blocked = integrity.blocking(findings, mode)
    verification: VerificationResult = {**combined, "attempts": attempts, "integrity": mode}
    verification["passed"] = combined["passed"] and not blocked
    if block := integrity.report_block(findings, mode):
        verification["report"] += f"\n\n{block}"
    step = next_step({**state, "verification": verification}, settings)
    if step == "exhausted":
        why = (
            f"Stopped after {attempts} fix attempt(s) (MAX_FIX_ATTEMPTS={settings.max_fix_attempts})."
            if attempts >= settings.max_fix_attempts
            else "Stopped: the whole-run budget cannot pay for another fix attempt."
        )
        verification["report"] += f"\n\n{why}"
    return {**update, "verification": verification, "stage": "verification" if step == "retry" else "report"}


def route_after_verdict(state: IdeaDiscussionState) -> Literal["pass", "retry", "exhausted"]:
    return next_step(state, get_settings())


# --------------------------------------------------------------------------- fix


async def fix_node(state: IdeaDiscussionState) -> dict[str, Any]:
    """One fix attempt on the merged failure list, committed by the orchestrator, then back to `verify`."""
    settings = get_settings()
    workspace = Path(state["workspace_dir"])
    verification = _previous(state)
    attempt = int(verification.get("attempts") or 0) + 1
    iteration = int(state.get("iteration") or 1)
    blocked = integrity.blocking(state.get("integrity_findings") or {}, settings.test_integrity)
    failures = lanes.failure_list(verification.get("lanes") or []) + integrity.failure_lines(blocked)
    failures = failures or [str(verification.get("report") or "unknown failure")]
    LOGGER.info("Verification failed; fix attempt %d/%d", attempt, settings.max_fix_attempts)
    budget = min(settings.implementer_max_task_usd, remaining_budget(state, settings))
    outcome = await lanes.run_fix(
        workspace,
        failures,
        settings,
        budget_usd=budget,
        emit=emitter(),
        attempt=attempt,
        iteration=iteration,
    )
    await asyncio.to_thread(implementer.commit_workspace, workspace, f"fix attempt {attempt}")
    update: dict[str, Any] = {"verification": {**verification, "attempts": attempt}, "stage": "verification"}
    if outcome.integrity:
        update["integrity_findings"] = {finding["id"]: finding for finding in outcome.integrity}
    if outcome.cost_usd > 0:
        update["usage"] = [_record("fixer", settings, outcome.cost_usd, lanes.fix_role(attempt))]
    return update

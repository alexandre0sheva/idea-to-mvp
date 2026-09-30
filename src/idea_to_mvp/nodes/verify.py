"""Verification as graph steps: `verify` fans out to three lanes, `verdict` folds them, `fix` repairs.

```
implement → verify ─Send×3→ verify_lane → verdict ─ pass / exhausted ─→ delivery_report
               ↑                              └──────── retry ──→ fix ─┘ (back to verify)
```

Each stage is its own node, so every fix attempt is a checkpoint and a stream event instead of a `while`
loop hidden inside one node. The loop ends when the lanes pass, when `MAX_FIX_ATTEMPTS` fixes were made,
or when the whole-run budget cannot pay for another round. Lanes and sessions are described in
`implementation/verify.py`.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Literal, TypedDict

from langgraph.types import Send

from idea_to_mvp import implementer
from idea_to_mvp.config import Settings, get_settings
from idea_to_mvp.implementation import verify as lanes
from idea_to_mvp.implementation.verify import LANES, LaneName, LaneReport
from idea_to_mvp.nodes.implement import emitter
from idea_to_mvp.state import IdeaDiscussionState, VerificationResult
from idea_to_mvp.usage import UsageRecord, summarize_usage

LOGGER = logging.getLogger(__name__)

_MIN_BUDGET_USD = 0.01  # below this no session can usefully start


class LaneInput(TypedDict):
    """Payload of one `Send("verify_lane", ...)`."""

    lane: LaneName
    workspace_dir: str
    budget_usd: float


def remaining_budget(state: IdeaDiscussionState, settings: Settings) -> float:
    """What is left of the whole-run cap after everything this version has spent (implementation, lanes,
    fixes). Earlier versions of an iterated project are not counted: each has its own budget."""
    spent = (summarize_usage(state.get("usage") or [])["cost_usd"] or 0.0) - float(state.get("spent_before_iteration") or 0.0)
    return max(0.0, settings.implementer_max_total_usd - spent)


def _record(role: str, settings: Settings, cost_usd: float) -> UsageRecord:
    return {
        "role": role,
        "provider": "anthropic",
        "model": settings.implementer_model,
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
    skipped: VerificationResult = {
        "passed": False,
        "attempts": int(previous.get("attempts") or 0),
        "report": report,
        "lanes": list(previous.get("lanes") or []),
    }
    return {"verification": skipped, "stage": "report"}


def route_after_verify(state: IdeaDiscussionState) -> list[Send] | Literal["delivery_report"]:
    settings = get_settings()
    remaining = remaining_budget(state, settings)
    if remaining < _MIN_BUDGET_USD:
        return "delivery_report"
    # The three lanes run at once, so each may spend the per-task cap or its share of what is left.
    budget = min(settings.implementer_max_task_usd, remaining / len(LANES))
    workspace = str(state["workspace_dir"])
    return [Send("verify_lane", LaneInput(lane=lane, workspace_dir=workspace, budget_usd=budget)) for lane in LANES]


async def verify_lane_node(state: LaneInput) -> dict[str, Any]:
    settings = get_settings()
    outcome = await lanes.run_lane(
        Path(state["workspace_dir"]), state["lane"], settings, budget_usd=state["budget_usd"], emit=emitter()
    )
    update: dict[str, Any] = {"lane_reports": {state["lane"]: outcome.report.model_dump()}}
    if outcome.cost_usd > 0:
        update["usage"] = [_record("verifier", settings, outcome.cost_usd)]
    return update


# ------------------------------------------------------------------------ verdict


def next_step(state: IdeaDiscussionState, settings: Settings) -> Literal["pass", "retry", "exhausted"]:
    verification = _previous(state)
    if verification.get("passed"):
        return "pass"
    if int(verification.get("attempts") or 0) >= settings.max_fix_attempts:
        return "exhausted"
    return "retry" if remaining_budget(state, settings) >= _MIN_BUDGET_USD else "exhausted"


def verdict_node(state: IdeaDiscussionState) -> dict[str, Any]:
    settings = get_settings()
    reports = [LaneReport.model_validate(report) for report in (state.get("lane_reports") or {}).values()]
    combined = lanes.combine_lanes(reports)
    attempts = int(_previous(state).get("attempts") or 0)
    verification: VerificationResult = {**combined, "attempts": attempts}
    step = next_step({**state, "verification": verification}, settings)
    if step == "exhausted":
        why = (
            f"Stopped after {attempts} fix attempt(s) (MAX_FIX_ATTEMPTS={settings.max_fix_attempts})."
            if attempts >= settings.max_fix_attempts
            else "Stopped: the whole-run budget cannot pay for another fix attempt."
        )
        verification["report"] += f"\n\n{why}"
    return {"verification": verification, "stage": "verification" if step == "retry" else "report"}


def route_after_verdict(state: IdeaDiscussionState) -> Literal["pass", "retry", "exhausted"]:
    return next_step(state, get_settings())


# --------------------------------------------------------------------------- fix


async def fix_node(state: IdeaDiscussionState) -> dict[str, Any]:
    """One fix attempt on the merged failure list, committed by the orchestrator, then back to `verify`."""
    settings = get_settings()
    workspace = Path(state["workspace_dir"])
    verification = _previous(state)
    attempt = int(verification.get("attempts") or 0) + 1
    failures = lanes.failure_list(verification.get("lanes") or []) or [str(verification.get("report") or "unknown failure")]
    LOGGER.info("Verification failed; fix attempt %d/%d", attempt, settings.max_fix_attempts)
    budget = min(settings.implementer_max_task_usd, remaining_budget(state, settings))
    outcome = await lanes.run_fix(workspace, failures, settings, budget_usd=budget, emit=emitter())
    await asyncio.to_thread(implementer.commit_workspace, workspace, f"fix attempt {attempt}")
    update: dict[str, Any] = {"verification": {**verification, "attempts": attempt}, "stage": "verification"}
    if outcome.cost_usd > 0:
        update["usage"] = [_record("fixer", settings, outcome.cost_usd)]
    return update

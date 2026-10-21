"""The implementation steps: prepare a stable workspace, then build the plan task by task.

Two nodes on purpose. `prepare_workspace` copies the blueprint pack once and its result is
checkpointed, so when a killed run is continued `implementer` finds the same workspace (and its
progress file) instead of starting from a fresh copy.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, cast

from langgraph.config import get_stream_writer

from idea_to_mvp import implementer
from idea_to_mvp.config import Settings, get_settings
from idea_to_mvp.implementation import executor
from idea_to_mvp.implementation.events import ImplEvent
from idea_to_mvp.implementation.integrity import IntegrityFinding
from idea_to_mvp.implementation.ledger import load_ledger, tuning_spend
from idea_to_mvp.implementation.progress import TaskResult
from idea_to_mvp.plan import Plan, iteration_of, load_plan
from idea_to_mvp.roles import resolve_agent_model
from idea_to_mvp.state import IdeaDiscussionState
from idea_to_mvp.usage import UsageRecord

LOGGER = logging.getLogger(__name__)

STRATEGY_TASK_MODE = (
    "agent_team"  # one fresh session per plan task; "subagents" keeps the lead session
)


def prepare_workspace_node(state: IdeaDiscussionState) -> dict[str, Any]:
    settings = get_settings()
    workspace = implementer.prepare_workspace(
        Path(state["project_bundle_dir"]), settings.projects_dir
    )
    LOGGER.info("Implementation workspace prepared at %s", workspace)
    return {"workspace_dir": str(workspace), "stage": "implementation"}


def emitter() -> Callable[[ImplEvent], None]:
    """Publish events on LangGraph's custom stream (a no-op outside a graph run)."""
    try:
        writer = get_stream_writer()
    except RuntimeError:
        return lambda event: None
    return lambda event: writer(dict(event))


def runnable_plan(workspace: Path) -> Plan | None:
    try:
        plan = load_plan((workspace / "plan.json").read_text(encoding="utf-8"))
    except OSError:
        plan = None
    if not executor.plan_is_runnable(plan):
        return None
    return plan


def agent_record(role: str, model: str, cost_usd: float) -> UsageRecord:
    """A usage record for an agent session (the SDK reports dollars, not tokens)."""
    return {
        "role": role,
        "provider": "anthropic",
        "model": model,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": cost_usd,
    }


def usage_records(
    results: list[TaskResult],
    settings: Settings,
    iteration: int | None = None,
    workspace: Path | None = None,
) -> list[UsageRecord]:
    """Usage records for task results. With `iteration`, only that version's tasks: earlier versions are
    already in the run's usage, and the engine returns every task's result (finished ones included).

    A task's cost includes what its merge resolver spent. With the `workspace` (whose ledger knows the
    resolver sessions) that part becomes a `resolver` record on the model the resolver ran on, and the
    task's own record keeps the rest; without it the resolver stays folded into the task."""
    resolvers: dict[str, list[tuple[float, str]]] = {}
    if workspace is not None:
        for row in load_ledger(workspace):
            if row["kind"] == "resolver" and row["task_id"]:
                resolvers.setdefault(row["task_id"], []).append((row["cost_usd"], row["model"]))
    task_model = resolve_agent_model(settings, "implementer")
    records: list[UsageRecord] = []
    for result in results:
        if not (
            result["cost_usd"] > 0
            and (iteration is None or iteration_of(result["task_id"]) == iteration)
        ):
            continue
        spent = result["cost_usd"]
        resolver = min(sum(cost for cost, _ in resolvers.get(result["task_id"], [])), spent)
        records.append(agent_record("implementer", task_model, spent - resolver))
        if resolver > 0:
            records.append(
                agent_record(
                    "resolver", resolvers[result["task_id"]][-1][1] or task_model, resolver
                )
            )
    return records


def integrity_findings(results: list[TaskResult], iteration: int) -> dict[str, IntegrityFinding]:
    """What this version's task and resolver sessions did to the tests, by finding id (the engines return
    every task's result, and earlier versions' findings are already in state)."""
    return {
        finding["id"]: cast(IntegrityFinding, finding)
        for result in results
        if iteration_of(result["task_id"]) == iteration
        for finding in result.get("integrity", [])
    }


def effective_parallel(state: IdeaDiscussionState, settings: Settings) -> int:
    """How many tasks may run at once: what the user picked at the implement gate, else the setting."""
    chosen = int((state.get("implement_decision") or {}).get("parallel") or 0)
    return chosen if chosen >= 1 else settings.implementer_max_parallel


def builds_task_by_task(state: IdeaDiscussionState) -> bool:
    """One fresh session per plan task: strategy `agent_team`, and always for an iteration (its new tasks
    are the only thing to build; a lead session would rebuild the whole plan)."""
    strategy = state.get("execution_strategy") or {}
    return (strategy.get("mode") or "subagents") == STRATEGY_TASK_MODE or int(
        state.get("iteration") or 1
    ) > 1


def route_after_workspace(state: IdeaDiscussionState) -> Literal["implementer", "implement_plan"]:
    """Independent tasks run in parallel worktrees (the `implement_plan` subgraph) when the strategy builds
    the plan task by task, parallelism is allowed, the plan can be scheduled, and the workspace is a git
    repository; everything else stays on the single-node implementer."""
    settings = get_settings()
    workspace = Path(state["workspace_dir"])
    parallel = (
        effective_parallel(state, settings) > 1
        and builds_task_by_task(state)
        and (workspace / ".git").is_dir()
        and runnable_plan(workspace) is not None
    )
    return "implement_plan" if parallel else "implementer"


async def implementer_node(state: IdeaDiscussionState) -> dict[str, Any]:
    settings = get_settings()
    workspace = Path(state["workspace_dir"])
    strategy = state.get("execution_strategy") or {}
    iteration = int(state.get("iteration") or 1)
    plan = runnable_plan(workspace) if builds_task_by_task(state) else None

    if plan is not None:
        results = await executor.run_plan(
            workspace, plan, settings, emit=emitter(), iteration=iteration
        )
        await asyncio.to_thread(implementer.commit_workspace, workspace, "implementation")
        return {
            "implementation_log": executor.render_task_log(plan, results),
            "task_results": {result["task_id"]: result for result in results},
            "usage": usage_records(results, settings, iteration, workspace),
            "integrity_findings": integrity_findings(results, iteration),
            "stage": "verification",
        }

    if builds_task_by_task(state):
        LOGGER.warning(
            "No usable plan.json in %s; building with workstream sessions instead.", workspace
        )
    # Harness tuning's pilot runs came out of this version's budget, as they do for the task engines.
    left = settings.implementer_max_total_usd - tuning_spend(workspace, iteration)
    if left <= 0:
        LOGGER.warning(
            "Harness tuning used up the whole-run budget; the lead session is not started."
        )
        return {
            "implementation_log": "Not started: harness tuning used up the whole-run budget (IMPLEMENTER_MAX_TOTAL_USD).",
            "stage": "verification",
        }
    lead_settings = (
        settings
        if left >= settings.implementer_max_total_usd
        else settings.model_copy(update={"implementer_max_total_usd": left})
    )
    before = len(load_ledger(workspace))
    log = await asyncio.to_thread(
        implementer.run_implementation, workspace, strategy, lead_settings
    )
    await asyncio.to_thread(
        implementer.commit_workspace, workspace, "implementation"
    )  # commits are the orchestrator's
    # The lead / workstream sessions just recorded in the ledger are this node's spend: count it in usage,
    # and so against the whole-run budget.
    sessions = [
        agent_record("implementer", row["model"], row["cost_usd"])
        for row in load_ledger(workspace)[before:]
        if row["cost_usd"] > 0
    ]
    return {"implementation_log": log, "usage": sessions, "stage": "verification"}

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
from typing import Any, Literal

from langgraph.config import get_stream_writer

from idea_to_mvp import implementer
from idea_to_mvp.config import Settings, get_settings
from idea_to_mvp.implementation import executor
from idea_to_mvp.implementation.events import ImplEvent
from idea_to_mvp.implementation.progress import TaskResult
from idea_to_mvp.plan import Plan, iteration_of, load_plan
from idea_to_mvp.state import IdeaDiscussionState
from idea_to_mvp.usage import UsageRecord

LOGGER = logging.getLogger(__name__)

STRATEGY_TASK_MODE = "agent_team"  # one fresh session per plan task; "subagents" keeps the lead session


def prepare_workspace_node(state: IdeaDiscussionState) -> dict[str, Any]:
    settings = get_settings()
    workspace = implementer.prepare_workspace(Path(state["project_bundle_dir"]), settings.projects_dir)
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


def usage_records(results: list[TaskResult], settings: Settings, iteration: int | None = None) -> list[UsageRecord]:
    """Usage records for task results. With `iteration`, only that version's tasks: earlier versions are
    already in the run's usage, and the engine returns every task's result (finished ones included)."""
    return [
        {
            "role": "implementer",
            "provider": "anthropic",
            "model": settings.implementer_model,
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": result["cost_usd"],
        }
        for result in results
        if result["cost_usd"] > 0 and (iteration is None or iteration_of(result["task_id"]) == iteration)
    ]


def effective_parallel(state: IdeaDiscussionState, settings: Settings) -> int:
    """How many tasks may run at once: what the user picked at the implement gate, else the setting."""
    chosen = int((state.get("implement_decision") or {}).get("parallel") or 0)
    return chosen if chosen >= 1 else settings.implementer_max_parallel


def builds_task_by_task(state: IdeaDiscussionState) -> bool:
    """One fresh session per plan task: strategy `agent_team`, and always for an iteration (its new tasks
    are the only thing to build; a lead session would rebuild the whole plan)."""
    strategy = state.get("execution_strategy") or {}
    return (strategy.get("mode") or "subagents") == STRATEGY_TASK_MODE or int(state.get("iteration") or 1) > 1


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
        results = await executor.run_plan(workspace, plan, settings, emit=emitter(), iteration=iteration)
        await asyncio.to_thread(implementer.commit_workspace, workspace, "implementation")
        return {
            "implementation_log": executor.render_task_log(plan, results),
            "task_results": {result["task_id"]: result for result in results},
            "usage": usage_records(results, settings, iteration),
            "stage": "verification",
        }

    if builds_task_by_task(state):
        LOGGER.warning("No usable plan.json in %s; building with workstream sessions instead.", workspace)
    log = await asyncio.to_thread(implementer.run_implementation, workspace, strategy, settings)
    await asyncio.to_thread(implementer.commit_workspace, workspace, "implementation")  # commits are the orchestrator's
    return {"implementation_log": log, "stage": "verification"}

"""Parallel implementation: independent plan tasks in git worktrees, merged back in task-id order.

```
START → prepare → pick_wave ─Send×k→ run_task_node → merge_wave ─┐
                     ↑   └──── nothing left to start ──→ finish → END
                     └──────────────────────────────────────────┘
```

- `pick_wave` marks tasks that can never run (a dependency failed) as skipped, then takes the ready
  tasks (dependencies merged) up to `IMPLEMENTER_MAX_PARALLEL` and as many as the remaining budget can
  cover at the per-task cap, and creates one worktree per task.
- `run_task_node` runs one fresh agent session in its worktree and commits the work on the task's branch.
  A task is *not* done yet: it only counts once merged.
- `merge_wave` merges the finished branches in task-id order. A conflict goes to one bounded resolver
  session in the main workspace; if that fails the merge is aborted and the task fails, and its dependents
  are skipped. Only then are results recorded (`progress.json`), so a kill between a session and its merge
  loses nothing already done.
- `finish` writes the log and the usage, commits, and removes leftover worktrees.

Parallelism is enforced by batch size: a node cannot set its own concurrency, so a batch never holds more
than `IMPLEMENTER_MAX_PARALLEL` tasks (LangGraph's `max_concurrency`, `LLM_MAX_CONCURRENCY`, caps the whole
run on top). Budget: every started task reserves the per-task cap, so the whole-run cap cannot be overshot
by tasks running at once. Project test commands are never run on the host (that would execute
agent-written code outside the sandbox): a task's own session runs its tests, the resolver session runs
them after a conflict, and verification covers the integrated result.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Send

from idea_to_mvp.config import Settings, get_settings
from idea_to_mvp.implementation import executor, merge
from idea_to_mvp.implementation.events import make_event
from idea_to_mvp.implementation.progress import (
    TaskResult,
    load_progress,
    save_result,
    spent_in_iteration,
)
from idea_to_mvp.implementation.scheduler import blocked_tasks, next_ready
from idea_to_mvp.implementation.workspace import commit_workspace
from idea_to_mvp.nodes.implement import effective_parallel, emitter, runnable_plan, usage_records
from idea_to_mvp.plan import Plan, PlanTask, execution_waves
from idea_to_mvp.state import IdeaDiscussionState

LOGGER = logging.getLogger(__name__)
_EPSILON = 1e-9


class ImplementInput(TypedDict):
    """What the parent hands the subgraph. Leaves out `usage`, `task_results`, `finished_tasks` and the
    log so the subgraph starts them empty and returns only what it produced (see `nodes/panel.py`)."""

    workspace_dir: str
    execution_strategy: dict[str, Any]
    implement_decision: dict[str, Any]  # carries the parallelism chosen at the implement gate
    iteration: int  # the version being built: its tasks share one whole-run budget


class TaskInput(TypedDict):
    """Payload of one `Send("run_task_node", ...)`."""

    task_id: str
    workspace_dir: str
    allocation: float  # the budget this task reserves: min(per-task cap, whole-run budget)


def _plan(workspace: Path) -> Plan:
    plan = runnable_plan(workspace)
    if plan is None:
        raise RuntimeError(f"{workspace} has no runnable plan.json")
    return plan


def _task_cap(settings: Settings) -> float:
    return min(settings.implementer_max_task_usd, settings.implementer_max_total_usd)


def _spent(workspace: Path, iteration: int, unrecorded: float = 0.0) -> float:
    """What this version's tasks have cost (earlier versions have their own budget)."""
    return spent_in_iteration(load_progress(workspace), iteration) + unrecorded


def _done_and_dead(workspace: Path, plan: Plan, current: dict[str, TaskResult]) -> tuple[set[str], set[str]]:
    """Done tasks come from disk (they survive restarts); dead ones only from *this* run's results, so
    tasks that failed in an earlier run are retried."""
    ids = {task.id for task in plan.tasks}
    done = {task_id for task_id, result in load_progress(workspace).items() if result["status"] == "done" and task_id in ids}
    dead = {task_id for task_id, result in current.items() if result["status"] in ("failed", "skipped")}
    return done, dead


def _next_batch(
    workspace: Path, plan: Plan, settings: Settings, done: set[str], dead: set[str], iteration: int, max_parallel: int
) -> tuple[list[PlanTask], int]:
    """(tasks to start now, slots the budget allows). A started task reserves the per-task cap."""
    ready = [task for task in next_ready(plan, done, running=set()) if task.id not in dead]
    cap = _task_cap(settings)
    affordable = int((settings.implementer_max_total_usd - _spent(workspace, iteration)) // cap + _EPSILON)
    slots = max(0, min(max_parallel, affordable))
    return ready[:slots], slots


def _failure(task_id: str, summary: str, status: Literal["failed", "skipped"] = "failed") -> TaskResult:
    return {"task_id": task_id, "status": status, "summary": summary, "cost_usd": 0.0, "turns": 0, "session_id": None, "commit": None}


# ------------------------------------------------------------------------ nodes


def prepare_node(state: IdeaDiscussionState) -> dict[str, Any]:
    """Discard worktrees and branches of a killed run: their sessions are redone from the current main."""
    workspace = Path(state["workspace_dir"])
    merge.remove_all_worktrees(workspace)
    return {}


async def pick_wave_node(state: IdeaDiscussionState) -> dict[str, Any]:
    settings = get_settings()
    workspace = Path(state["workspace_dir"])
    plan = _plan(workspace)
    emit = emitter()
    tasks = {task.id: task for task in plan.tasks}
    done, dead = _done_and_dead(workspace, plan, dict(state.get("task_results") or {}))
    updates: dict[str, TaskResult] = {
        task_id: result for task_id, result in load_progress(workspace).items() if task_id in done
    }

    def settle(result: TaskResult, detail: str) -> None:
        save_result(workspace, result)
        updates[result["task_id"]] = result
        dead.add(result["task_id"])
        emit(make_event("task_end", task_id=result["task_id"], label=tasks[result["task_id"]].title, detail=detail))

    def skip_blocked() -> None:
        for task_id, waiting_on in blocked_tasks(plan, dead, done | dead).items():
            summary = f"Skipped: depends on unfinished task(s) {', '.join(waiting_on)}."
            settle(_failure(task_id, summary, "skipped"), f"skipped: waiting on {', '.join(waiting_on)}")

    skip_blocked()
    iteration = int(state.get("iteration") or 1)
    batch, slots = _next_batch(workspace, plan, settings, done, dead, iteration, effective_parallel(state, settings))
    ready = [task for task in next_ready(plan, done, running=set()) if task.id not in dead]
    if ready and slots == 0:
        left = max(0.0, settings.implementer_max_total_usd - _spent(workspace, iteration))
        for task in ready:
            summary = (
                f"Not started: the whole-run budget has ${left:.2f} left, less than the ${_task_cap(settings):.2f} "
                "a task can need (IMPLEMENTER_MAX_TOTAL_USD)."
            )
            settle(_failure(task.id, summary), "failed: whole-run budget used up")
        skip_blocked()
        batch = []
    for task in batch:  # one at a time: git takes locks in the shared repository
        await merge.create_worktree(workspace, task.id)
    if batch:
        names = ", ".join(task.id for task in batch)
        emit(make_event("text", label="scheduler", detail=f"Starting {names}" + (" in parallel" if len(batch) > 1 else "")))
    return {"task_results": updates}


def route_after_pick(state: IdeaDiscussionState) -> list[Send] | Literal["finish"]:
    settings = get_settings()
    workspace = Path(state["workspace_dir"])
    plan = _plan(workspace)
    done, dead = _done_and_dead(workspace, plan, dict(state.get("task_results") or {}))
    batch, _ = _next_batch(
        workspace, plan, settings, done, dead, int(state.get("iteration") or 1), effective_parallel(state, settings)
    )
    if not batch:
        return "finish"
    allocation = _task_cap(settings)
    return [
        Send("run_task_node", TaskInput(task_id=task.id, workspace_dir=str(workspace), allocation=allocation))
        for task in batch
    ]


async def run_task_node(state: TaskInput) -> dict[str, Any]:
    settings = get_settings()
    workspace = Path(state["workspace_dir"])
    plan = _plan(workspace)
    task = next(task for task in plan.tasks if task.id == state["task_id"])
    result = await executor.run_task(
        workspace,
        task,
        settings,
        budget=executor.BudgetTracker(state["allocation"]),
        emit=emitter(),
        plan=plan,
        work_dir=merge.worktree_path(workspace, task.id),
        persist=False,
        announce_end=False,
    )
    return {"finished_tasks": {task.id: result}}


async def merge_wave_node(state: IdeaDiscussionState) -> dict[str, Any]:
    settings = get_settings()
    workspace = Path(state["workspace_dir"])
    plan = _plan(workspace)
    emit = emitter()
    iteration = int(state.get("iteration") or 1)
    tasks = {task.id: task for task in plan.tasks}
    finished = dict(state.get("finished_tasks") or {})
    recorded = state.get("task_results") or {}
    pending = sorted(task_id for task_id in finished if task_id not in recorded)
    unrecorded = {task_id: finished[task_id]["cost_usd"] for task_id in pending}  # spent, not yet in progress.json
    cap = _task_cap(settings)
    updates: dict[str, TaskResult] = {}

    def settle(result: TaskResult) -> None:
        save_result(workspace, result)
        updates[result["task_id"]] = result
        unrecorded.pop(result["task_id"], None)
        detail = "done" if result["status"] == "done" else f"failed: {result['summary'].splitlines()[0][:120]}"
        emit(make_event("task_end", task_id=result["task_id"], label=tasks[result["task_id"]].title, detail=detail))

    async def resolve(task: PlanTask, result: TaskResult, files: list[str]) -> tuple[str, str | None]:
        """Try to resolve a conflicted merge: (failure reason or '', merge commit)."""
        budget_usd = min(cap, settings.implementer_max_total_usd - _spent(workspace, iteration, sum(unrecorded.values())))
        if budget_usd <= 0:
            return "merge conflict, and no whole-run budget is left for a resolver", None
        resolver = await executor.run_merge_resolver(
            workspace, task, files, settings, emit=emit, budget_usd=budget_usd, plan=plan
        )
        result["cost_usd"] += resolver.cost_usd
        unrecorded[task.id] = unrecorded.get(task.id, 0.0) + resolver.cost_usd
        if not resolver.success:
            reason = resolver.summary.splitlines()[0][:160] if resolver.summary else "no reason given"
            return f"merge conflict could not be resolved: {reason}", None
        done = await merge.complete_merge(workspace, f"{task.id}: {task.title} (conflicts resolved)", task.id)
        if done.status != "merged":
            return f"merge conflicts remained after the resolver ({done.detail})", None
        return "", done.commit

    for task_id in pending:
        task = tasks[task_id]
        result: TaskResult = {**finished[task_id]}
        if result["status"] == "done":
            outcome = await merge.merge_task(workspace, task_id, message=f"{task_id}: {task.title} (merged)")
            if outcome.status == "merged":
                result["commit"] = outcome.commit
            else:
                files = merge.conflicted_files(workspace)
                emit(make_event("text", task_id=task_id, label="merge", detail=f"Merge conflict in {', '.join(files)}: starting a resolver"))
                failure, commit = await resolve(task, result, files)
                if failure:
                    await merge.abort_merge(workspace)
                    result = {**result, "status": "failed", "summary": f"{failure}. {result['summary']}".strip(), "commit": None}
                else:
                    note = f"(Merge conflicts in {', '.join(files)} were resolved by an agent.)"
                    result = {**result, "summary": f"{result['summary']} {note}", "commit": commit}
        await merge.remove_worktree(workspace, task_id)
        settle(result)
    return {"task_results": updates}


def finish_node(state: IdeaDiscussionState) -> dict[str, Any]:
    settings = get_settings()
    workspace = Path(state["workspace_dir"])
    plan = _plan(workspace)
    current = dict(state.get("task_results") or {})
    progress = load_progress(workspace)
    ordered = [task_id for wave in execution_waves(plan) for task_id in wave]
    results: list[TaskResult] = []
    for task_id in ordered:
        result = current.get(task_id) or progress.get(task_id)
        if result is None:
            result = _failure(task_id, "Not run.", "skipped")
            save_result(workspace, result)
        results.append(result)
    merge.remove_all_worktrees(workspace)
    commit_workspace(workspace, "implementation")
    return {
        "implementation_log": executor.render_task_log(plan, results),
        "task_results": {result["task_id"]: result for result in results},
        "usage": usage_records(results, settings, int(state.get("iteration") or 1)),
        "stage": "verification",
    }


def build_implement_subgraph() -> CompiledStateGraph:
    """The parallel engine, compiled to be mounted as the parent graph's `implement_plan` node."""
    builder = StateGraph(IdeaDiscussionState, input_schema=ImplementInput)
    builder.add_node("prepare", prepare_node)
    builder.add_node("pick_wave", pick_wave_node)
    builder.add_node("run_task_node", run_task_node)
    builder.add_node("merge_wave", merge_wave_node)
    builder.add_node("finish", finish_node)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "pick_wave")
    builder.add_conditional_edges("pick_wave", route_after_pick, ["run_task_node", "finish"])
    builder.add_edge("run_task_node", "merge_wave")
    builder.add_edge("merge_wave", "pick_wave")
    builder.add_edge("finish", END)
    return builder.compile()

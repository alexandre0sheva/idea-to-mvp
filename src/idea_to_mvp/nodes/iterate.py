"""The iteration loop: after a delivery the user can ask for changes and get the next version.

```
delivery_report ─(iteration < MAX_ITERATIONS)→ iterate_gate ─ iterate ─→ change_planner → implement → verify → delivery_report
                └────────────── otherwise ───→ END              └─ done ─→ END
```

The gate records the feedback and moves `iteration` on (v0.1 → v0.2). `change_planner` turns the feedback into
new plan tasks (`I2-01`, ...) appended to `plan.json` without touching finished tasks, so the ordinary
engine builds only those: tasks already `done` in `progress.json` are skipped. Each iteration starts
verification and the whole-run budget from scratch.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.types import interrupt

from idea_to_mvp import llm
from idea_to_mvp.config import get_settings
from idea_to_mvp.implementation.progress import TaskResult, load_progress, save_result
from idea_to_mvp.implementation.workspace import commit_workspace
from idea_to_mvp.plan import (
    ChangePlan,
    Plan,
    PlanTask,
    append_iteration_tasks,
    fallback_change_tasks,
    iteration_prefix,
    load_plan,
    render_plan_markdown,
    validate_plan,
    workstream_names,
)
from idea_to_mvp.state import IdeaDiscussionState, IterateDecision, VerificationResult
from idea_to_mvp.usage import summarize_usage, with_usage

LOGGER = logging.getLogger(__name__)

__all__ = ["ChangePlan", "change_planner_node", "iterate_gate_node", "route_after_delivery", "route_after_iterate_gate"]

_PRD_LIMIT = 20000


def route_after_delivery(state: IdeaDiscussionState) -> Literal["iterate_gate", "__end__"]:
    """Offer another iteration only while the version limit (`MAX_ITERATIONS`) allows one more."""
    if int(state.get("iteration") or 1) < get_settings().max_iterations:
        return "iterate_gate"
    return "__end__"


def iterate_gate_node(state: IdeaDiscussionState) -> dict[str, Any]:
    settings = get_settings()
    iteration = int(state.get("iteration") or 1)
    passed = bool((state.get("verification") or {}).get("passed"))
    question = (
        f"Version v0.{iteration} is delivered (verification {'passed' if passed else 'did not pass'}). "
        f"Want changes? Describe what to add or fix: the change planner turns it into new tasks, the agents "
        f"build them in the same project, verification runs again, and you get v0.{iteration + 1}. This "
        f"spends API tokens again (the whole-run budget applies to each version). "
        f"{settings.max_iterations - iteration} more version(s) can follow this one."
    )
    decision = interrupt(
        {
            "kind": "iterate_gate",
            "report": state.get("delivery_report", ""),
            "iteration": iteration,
            "question": question,
            "passed": passed,
            "delivery_zip": state.get("delivery_zip", ""),
            "max_iterations": settings.max_iterations,
        }
    )
    iterate, feedback = False, ""
    if isinstance(decision, dict):
        feedback = str(decision.get("feedback") or "").strip()
        iterate = bool(decision.get("iterate")) and bool(feedback)  # changes need a description
    iterate_decision: IterateDecision = {"iterate": iterate, "feedback": feedback if iterate else ""}
    if not iterate:
        return {"iterate_decision": iterate_decision, "stage": "done"}
    return {
        "iterate_decision": iterate_decision,
        "iteration": iteration + 1,
        "change_requests": [*(state.get("change_requests") or []), feedback],
        "stage": "implementation",
    }


def route_after_iterate_gate(state: IdeaDiscussionState) -> Literal["change_planner", "__end__"]:
    if (state.get("iterate_decision") or {}).get("iterate"):
        return "change_planner"
    return "__end__"


# ------------------------------------------------------------------ planner


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _task_lines(plan: Plan, progress: dict[str, TaskResult]) -> str:
    lines = []
    for task in plan.tasks:
        result = progress.get(task.id)
        status = result["status"] if result else "not built"
        deps = f"; depends on {', '.join(task.depends_on)}" if task.depends_on else ""
        reqs = f"; requirements {', '.join(task.requirement_ids)}" if task.requirement_ids else ""
        lines.append(f"- {task.id} [{status}] {task.title} (workstream {task.workstream}{deps}{reqs})")
    return "\n".join(lines) or "- (none)"


def _prompt(plan: Plan, prd: str, feedback: str, iteration: int, workstreams: list[str], progress: dict[str, TaskResult]) -> str:
    prefix = iteration_prefix(iteration)
    contracts = "\n".join(f"- {c.id}: {c.name} — {c.description}" for c in plan.contracts) or "- none"
    return (
        f"## Change request (building v0.{iteration})\n{feedback}\n\n"
        f"## New task ids\nUse {prefix}01, {prefix}02, ... in execution order.\n\n"
        f"## Workstreams\n{json.dumps([{'name': name} for name in workstreams])}\n\n"
        f"## Existing tasks (already planned; finished ones must not change)\n{_task_lines(plan, progress)}\n\n"
        f"## Contract registry\n{contracts}\n\n"
        f"## Project commands\ninstall: {plan.commands.install}\ntest: {plan.commands.test}\n"
        f"lint: {plan.commands.lint}\nrun: {plan.commands.run}\n\n"
        f"## PRD.md\n{prd[:_PRD_LIMIT]}"
    )


def _change_issues(plan: Plan, tasks: list[PlanTask], iteration: int, prd: str, workstreams: list[str], baseline: set[str]) -> list[str]:
    """Everything wrong with the change (problems the plan already had before it are not held against it)."""
    if not tasks:
        return ["the change plan has no tasks"]
    prefix = iteration_prefix(iteration)
    expected = [f"{prefix}{number:02d}" for number in range(1, len(tasks) + 1)]
    issues: list[str] = []
    if [task.id for task in tasks] != expected:
        issues.append(f"task ids must be {', '.join(expected)} in order (got {', '.join(task.id for task in tasks)})")
    try:
        extended = append_iteration_tasks(plan, tasks)
    except ValueError as error:
        return [*issues, str(error)]
    return [*issues, *(i for i in validate_plan(extended, prd_markdown=prd, workstreams=workstreams) if i not in baseline)]


def _mark_first_delivery_done(workspace: Path, plan: Plan) -> None:
    """A lead session builds the whole first version without per-task progress; record its tasks as done so
    the task engine builds only the new ones."""
    progress = load_progress(workspace)
    for task in plan.tasks:
        if task.id not in progress:
            save_result(
                workspace,
                {
                    "task_id": task.id,
                    "status": "done",
                    "summary": "Built in the first delivery by the lead session.",
                    "cost_usd": 0.0,
                    "turns": 0,
                    "session_id": None,
                    "commit": None,
                },
            )


@with_usage(role="change_planner")
def change_planner_node(state: IdeaDiscussionState) -> dict[str, Any]:
    """Plan the change request as new tasks and append them to the workspace's `plan.json` / `plan.md`.

    One repair attempt with the validator's findings; if the plan is still unusable, one deterministic task
    that carries the request. The plan is committed so task worktrees (made from HEAD) contain it.
    """
    workspace = Path(state["workspace_dir"])
    iteration = int(state.get("iteration") or 2)
    feedback = (state.get("change_requests") or [""])[-1]
    plan = load_plan(_read(workspace / "plan.json"))
    if plan is None:
        raise RuntimeError(f"{workspace} has no valid plan.json to extend, so the changes cannot be planned.")
    strategy = state.get("execution_strategy") or {}
    if (strategy.get("mode") or "subagents") != "agent_team":
        _mark_first_delivery_done(workspace, plan)
    prd = _read(workspace / "PRD.md")
    workstreams = workstream_names(strategy)
    baseline = set(validate_plan(plan, prd_markdown=prd, workstreams=workstreams))

    runtime = llm.get_runtime("change_planner")
    messages = [
        SystemMessage(content=runtime.system_prompt),
        HumanMessage(content=_prompt(plan, prd, feedback, iteration, workstreams, load_progress(workspace))),
    ]
    change = llm.invoke_structured(
        runtime,
        messages,
        ChangePlan,
        fallback=lambda: ChangePlan(tasks=fallback_change_tasks(plan, iteration, feedback)),
    )
    issues = _change_issues(plan, change.tasks, iteration, prd, workstreams, baseline)
    if issues:
        change = llm.invoke_structured(
            runtime,
            [
                *messages,
                HumanMessage(
                    content=(
                        "Your change plan has these problems:\n"
                        + "\n".join(f"- {issue}" for issue in issues)
                        + f"\n\nYour change plan (JSON):\n{change.model_dump_json(indent=2)}\n\n"
                        "Return the complete corrected change plan: fix every problem and change nothing else."
                    )
                ),
            ],
            ChangePlan,
            fallback=lambda: change,
        )
        issues = _change_issues(plan, change.tasks, iteration, prd, workstreams, baseline)
    if issues:
        LOGGER.warning("Change plan stayed invalid (%s); using the deterministic fallback task.", "; ".join(issues))
        change = ChangePlan(tasks=fallback_change_tasks(plan, iteration, feedback))

    extended = append_iteration_tasks(plan, change.tasks)
    (workspace / "plan.json").write_text(extended.model_dump_json(indent=2), encoding="utf-8")
    (workspace / "plan.md").write_text(render_plan_markdown(extended), encoding="utf-8")
    commit_workspace(workspace, f"I{iteration}: plan")
    fresh: VerificationResult = {"passed": False, "attempts": 0, "report": "", "lanes": []}
    return {
        "verification": fresh,
        "spent_before_iteration": summarize_usage(state.get("usage") or [])["cost_usd"] or 0.0,
        "stage": "implementation",
    }

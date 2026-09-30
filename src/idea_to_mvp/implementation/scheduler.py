"""Which plan tasks can start, given what is done and what is running (pure, no I/O)."""

from __future__ import annotations

from collections.abc import Collection

from idea_to_mvp.plan import Plan, PlanTask, execution_waves


def next_ready(plan: Plan, done: Collection[str], running: Collection[str]) -> list[PlanTask]:
    """Tasks whose dependencies are all done and that are neither done nor running, in task-id order."""
    finished = set(done)
    busy = finished | set(running)
    return [
        task
        for task in sorted(plan.tasks, key=lambda t: t.id)
        if task.id not in busy and all(dependency in finished for dependency in task.depends_on)
    ]


def blocked_tasks(plan: Plan, dead: Collection[str], finished: Collection[str]) -> dict[str, list[str]]:
    """Unfinished tasks that can never run because a dependency failed or was skipped (transitively).

    Maps each such task to the dead dependencies it is waiting on directly.
    """
    dead_ids = set(dead)
    settled = set(finished)
    blocked: dict[str, list[str]] = {}
    changed = True
    while changed:
        changed = False
        for task in sorted(plan.tasks, key=lambda t: t.id):
            if task.id in settled or task.id in blocked:
                continue
            waiting_on = [dependency for dependency in task.depends_on if dependency in dead_ids]
            if waiting_on:
                blocked[task.id] = waiting_on
                dead_ids.add(task.id)
                changed = True
    return blocked


def dag_width(plan: Plan) -> int:
    """Size of the largest layer of independent tasks (0 for an empty or cyclic plan)."""
    try:
        return max((len(wave) for wave in execution_waves(plan)), default=0)
    except ValueError:
        return 0

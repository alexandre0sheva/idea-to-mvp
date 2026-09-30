"""Builders for plans in tests (shared by test_plan.py and test_blueprint_dag.py)."""

from __future__ import annotations

from typing import Any

from idea_to_mvp.plan import Contract, Plan, PlanTask, ProjectCommands

PRD = "# PRD\n\n## Requirements\n- R1 (P0): log a climb\n- R2 (P0): see history\n- R3 (P1): export\n"
WORKSTREAMS = ["backend-api", "web-ui"]


def make_task(task_id: str = "T01", **overrides: Any) -> PlanTask:
    fields: dict[str, Any] = {
        "id": task_id,
        "title": f"Task {task_id}",
        "goal": "Deliver a slice.",
        "workstream": "backend-api",
        "depends_on": [],
        "requirement_ids": ["R1"],
        "contracts_in": [],
        "contracts_out": [],
        "scope": "Code and tests for the slice.",
        "acceptance": ["The slice works."],
        "tests": ["One test per acceptance criterion."],
        "coverage_target": 80,
        "handoff": "Notes.",
    }
    fields.update(overrides)
    return PlanTask(**fields)


def make_plan(tasks: list[PlanTask] | None = None, **overrides: Any) -> Plan:
    fields: dict[str, Any] = {
        "contracts": [Contract(id="C1", name="Core API", description="The shared entry contract.")],
        "tasks": tasks
        if tasks is not None
        else [
            make_task("T01", requirement_ids=["R1", "R2"], contracts_out=["C1"]),
            make_task("T02", workstream="web-ui", depends_on=["T01"], requirement_ids=["R1"], contracts_in=["C1"]),
        ],
        "commands": ProjectCommands(install="pip install -e .", test="pytest", lint=None, run=None),
    }
    fields.update(overrides)
    return Plan(**fields)

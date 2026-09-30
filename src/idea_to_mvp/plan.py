"""The machine-readable execution plan: typed tasks, a consistency validator, and execution waves.

`plan.json` is the source of truth of a blueprint pack and `plan.md` is rendered from it, so later
stages (scheduling, per-task resume, progress, verification) work on a task graph instead of prose.
This module is LLM-free; the planner node in `nodes/blueprint.py` produces a `Plan` and feeds the
validator's findings back for one repair attempt.

Schema rules (same as `schemas.py`): every field is required and there are no defaults, because some
providers reject optional fields in strict structured-output mode. Lists may simply be empty.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from graphlib import CycleError, TopologicalSorter
from typing import Any

from pydantic import BaseModel, ValidationError

TASK_ID_RE = re.compile(r"^(T\d+|I\d+-\d+)$")  # T01... (first delivery), I2-01... (iteration 2)
_ITERATION_ID_RE = re.compile(r"^I(\d+)-\d+$")
_REQUIREMENT_RE = re.compile(r"\bR\d+\b")
_PRIORITY_RE = re.compile(r"\bP[0-3]\b")
DEFAULT_WORKSTREAM = "core-product"
UNSET_TEST_COMMAND = "run the project's test suite (document the exact command in README.md)"


class Contract(BaseModel):
    id: str  # "C1"
    name: str
    description: str


class PlanTask(BaseModel):
    id: str  # "T01"
    title: str
    goal: str
    workstream: str
    depends_on: list[str]
    requirement_ids: list[str]  # "R1"... as numbered in PRD.md
    contracts_in: list[str]
    contracts_out: list[str]
    scope: str
    acceptance: list[str]
    tests: list[str]
    coverage_target: int  # percent of changed scope
    handoff: str


class ProjectCommands(BaseModel):
    install: str | None
    test: str
    lint: str | None
    run: str | None


class Plan(BaseModel):
    contracts: list[Contract]
    tasks: list[PlanTask]
    commands: ProjectCommands


class ChangePlan(BaseModel):
    """What the change planner returns: only the NEW tasks of an iteration (ids `I<n>-01`, ...)."""

    tasks: list[PlanTask]


def load_plan(text: str | None) -> Plan | None:
    """Parse `plan.json` content; None when it is missing or not a valid plan."""
    try:
        return Plan.model_validate_json(text) if text and text.strip() else None
    except (ValidationError, ValueError):
        return None


# ---------------------------------------------------------------- PRD parsing


def parse_requirements(prd_markdown: str) -> dict[str, str]:
    """Requirement ids found in the PRD, each with its priority ('P0', 'P1', ... or '').

    Works on the usual layouts (`- R1 (P0): ...`, `**R1** (P0)`, `| R1 | ... | P0 |`): the priority is
    the first `P<n>` on the line where an id first appears, and a later mention never overrides it.
    """
    requirements: dict[str, str] = {}
    for line in prd_markdown.splitlines():
        ids = _REQUIREMENT_RE.findall(line)
        if not ids:
            continue
        priority = _PRIORITY_RE.search(line)
        for position, requirement in enumerate(ids):
            if requirement not in requirements:
                requirements[requirement] = priority.group(0) if priority and position == 0 else ""
    return requirements


def workstream_names(strategy: Mapping[str, Any] | None) -> list[str]:
    names = [str(w.get("name") or "").strip() for w in (strategy or {}).get("workstreams") or []]
    return [name for name in names if name] or [DEFAULT_WORKSTREAM]


# ----------------------------------------------------------------- validation


def _cycle_text(error: CycleError) -> str:
    path = error.args[1] if len(error.args) > 1 else []
    return " -> ".join(str(node) for node in path)


def validate_plan(plan: Plan, *, prd_markdown: str, workstreams: list[str]) -> list[str]:
    """Everything wrong with the plan, one sentence each ([] when consistent)."""
    issues: list[str] = []
    if not plan.tasks:
        issues.append("the plan has no tasks")

    seen: set[str] = set()
    for task in plan.tasks:
        if task.id in seen:
            issues.append(f"duplicate task id {task.id}")
        seen.add(task.id)
        if not TASK_ID_RE.match(task.id):
            issues.append(f"malformed task id {task.id!r} (expected T01, T02, ..., or I2-01, ... for iteration tasks)")

    contract_ids = {contract.id for contract in plan.contracts}
    requirements = parse_requirements(prd_markdown)
    covered: set[str] = set()
    graph: dict[str, list[str]] = {}
    for task in plan.tasks:
        unknown_tasks = [dep for dep in task.depends_on if dep not in seen]
        if unknown_tasks:
            issues.append(f"task {task.id} depends on unknown task(s): {', '.join(unknown_tasks)}")
        graph[task.id] = [dep for dep in task.depends_on if dep in seen]
        if task.workstream not in workstreams:
            issues.append(f"task {task.id} uses unknown workstream {task.workstream!r}")
        unknown_contracts = [c for c in [*task.contracts_in, *task.contracts_out] if c not in contract_ids]
        if unknown_contracts:
            issues.append(f"task {task.id} references unknown contract(s): {', '.join(unknown_contracts)}")
        missing = [r for r in task.requirement_ids if r not in requirements]
        if missing:
            issues.append(f"task {task.id} cites requirement(s) not in the PRD: {', '.join(missing)}")
        covered.update(task.requirement_ids)

    try:
        TopologicalSorter(graph).prepare()
    except CycleError as error:
        issues.append(f"dependency cycle: {_cycle_text(error)}")

    used = {task.workstream for task in plan.tasks}
    for name in workstreams:
        if name not in used:
            issues.append(f"workstream {name!r} has no tasks")

    for requirement, priority in requirements.items():
        if priority == "P0" and requirement not in covered:
            issues.append(f"P0 requirement {requirement} is covered by no task")

    if not plan.commands.test.strip():
        issues.append("the project test command is empty")
    return issues


def iteration_of(task_id: str) -> int:
    """The version a task belongs to: `T01` is built for v0.1, `I2-01` for v0.2 (iteration 2)."""
    match = _ITERATION_ID_RE.match(task_id)
    return int(match.group(1)) if match else 1


def iteration_prefix(iteration: int) -> str:
    return f"I{iteration}-"


def append_iteration_tasks(plan: Plan, tasks: list[PlanTask]) -> Plan:
    """The plan extended with an iteration's tasks. Existing tasks, contracts, and commands are untouched;
    a new task may depend on existing ones but never reuse an id (finished tasks are not rewritten)."""
    existing = {task.id for task in plan.tasks}
    reused = [task.id for task in tasks if task.id in existing]
    if reused:
        raise ValueError(f"iteration tasks may not reuse existing task ids: {', '.join(reused)}")
    return plan.model_copy(update={"tasks": [*plan.tasks, *tasks]})


def fallback_change_tasks(plan: Plan, iteration: int, feedback: str) -> list[PlanTask]:
    """One deterministic task that carries the user's change request, for when the model's plan is unusable."""
    request = " ".join(feedback.split())
    workstream = plan.tasks[-1].workstream if plan.tasks else DEFAULT_WORKSTREAM
    return [
        PlanTask(
            id=f"{iteration_prefix(iteration)}01",
            title=f"Change request: {request[:60]}",
            goal=f"Make this change to the delivered product: {request}",
            workstream=workstream,
            depends_on=[],
            requirement_ids=[],
            contracts_in=[],
            contracts_out=[],
            scope="Whatever the change needs, in the existing code base; keep everything already delivered working.",
            acceptance=[f"The requested change works: {request}", "Everything delivered before still works."],
            tests=["A test for the requested behaviour; the existing test suite stays green."],
            coverage_target=80,
            handoff="Updated README and notes if the change affects how the project is run.",
        )
    ]


def execution_waves(plan: Plan) -> list[list[str]]:
    """Topological layers of task ids: every task in a layer can run once the previous layers are done."""
    sorter = TopologicalSorter({task.id: [d for d in task.depends_on] for task in plan.tasks})
    try:
        sorter.prepare()
    except CycleError as error:
        raise ValueError(f"the plan has a dependency cycle: {_cycle_text(error)}") from error
    waves: list[list[str]] = []
    while sorter.is_active():
        ready = sorted(sorter.get_ready())
        waves.append(ready)
        sorter.done(*ready)
    return waves


# ------------------------------------------------------------------ rendering


def _ids(values: list[str]) -> str:
    return ", ".join(values) if values else "none"


def render_plan_markdown(plan: Plan) -> str:
    """plan.md: the registry, then one block per task in the field layout the old prose plan used."""
    lines = ["## Contract Registry", ""]
    lines += [f"- {c.id}: {c.name} — {c.description}" for c in plan.contracts] or ["- none"]
    lines += ["", "## Tasks", ""]
    for task in plan.tasks:
        lines += [
            f"### {task.id}. {task.title}",
            f"- Goal: {task.goal}",
            f"- Workstream: {task.workstream}.",
            f"- Depends on: {_ids(task.depends_on)}.",
            f"- Requirements: {_ids(task.requirement_ids)}.",
            f"- Contracts in: {_ids(task.contracts_in)}.",
            f"- Contracts out: {_ids(task.contracts_out)}.",
            f"- Implementation scope: {task.scope}",
            "- Acceptance criteria:",
            *[f"  - {item}" for item in task.acceptance],
            "- Required unit/integration tests:",
            *[f"  - {item}" for item in task.tests],
            f"- Coverage target: at least {task.coverage_target}% for changed scope.",
            f"- Handoff artifacts: {task.handoff.strip() or 'none.'}",
            "",
        ]
    commands = plan.commands
    lines += [
        "## Project Commands",
        "",
        f"- Install: {_command(commands.install)}",
        f"- Test: {_command(commands.test)}",
        f"- Lint: {_command(commands.lint)}",
        f"- Run: {_command(commands.run)}",
    ]
    return "\n".join(lines) + "\n"


def _command(command: str | None) -> str:
    return f"`{command.strip()}`" if command and command.strip() else "none"


# --------------------------------------------------------------- plan.md -> Plan


class PlanParseError(ValueError):
    """`plan.md` could not be read back into a plan; `issues` says what is wrong, one sentence each."""

    def __init__(self, issues: list[str]) -> None:
        super().__init__("; ".join(issues))
        self.issues = issues


_TASK_FIELDS = {
    "Goal": "goal",
    "Workstream": "workstream",
    "Depends on": "depends_on",
    "Requirements": "requirement_ids",
    "Contracts in": "contracts_in",
    "Contracts out": "contracts_out",
    "Implementation scope": "scope",
    "Acceptance criteria": "acceptance",
    "Required unit/integration tests": "tests",
    "Coverage target": "coverage",
    "Handoff artifacts": "handoff",
}
_LIST_FIELDS = {"acceptance", "tests"}
_ID_LIST_FIELDS = {"depends_on", "requirement_ids", "contracts_in", "contracts_out"}
_FIELD_LINE = re.compile(r"^- (" + "|".join(re.escape(label) for label in _TASK_FIELDS) + r"):\s*(.*)$")
_TASK_HEADING = re.compile(r"^###\s+(\S+?)\.\s+(.*\S)\s*$")
_COMMAND_LINE = re.compile(r"^- (Install|Test|Lint|Run):\s*(.*)$")


def _split_ids(value: str) -> list[str]:
    value = value.strip().rstrip(".").strip()
    return [] if value.lower() in ("", "none") else [part.strip() for part in value.split(",") if part.strip()]


def _parse_command(value: str) -> str | None:
    value = value.strip()
    if value.lower() == "none" or not value:
        return None
    return value[1:-1] if value.startswith("`") and value.endswith("`") and len(value) >= 2 else value


def parse_plan_markdown(text: str) -> Plan:
    """Read a (possibly hand-edited) `plan.md` in the layout `render_plan_markdown` writes back into a `Plan`.

    Fields may continue on following lines. Raises `PlanParseError` listing every structural problem;
    whether the plan is *consistent* (cycles, unknown ids, ...) is `validate_plan`'s job.
    """
    issues: list[str] = []
    contracts: list[Contract] = []
    tasks: list[PlanTask] = []
    commands: dict[str, str | None] = {}
    section = ""
    task_id = ""
    fields: dict[str, Any] = {}
    field = ""  # the field the current line continues

    def finish_task() -> None:
        if not task_id:
            return
        missing = [label for label, key in _TASK_FIELDS.items() if key not in fields and key not in ("handoff",)]
        if missing:
            issues.append(f"task {task_id} is missing: {', '.join(missing)}")
            return
        coverage = re.search(r"(\d+)\s*%", str(fields["coverage"]))
        if coverage is None:
            issues.append(f"task {task_id}: 'Coverage target' needs a percentage (for example 'at least 80%')")
            return
        handoff = str(fields.get("handoff", "")).strip()
        tasks.append(
            PlanTask(
                id=task_id,
                title=str(fields["title"]),
                goal=str(fields["goal"]).strip(),
                workstream=str(fields["workstream"]).strip().rstrip(".").strip(),
                depends_on=_split_ids(str(fields["depends_on"])),
                requirement_ids=_split_ids(str(fields["requirement_ids"])),
                contracts_in=_split_ids(str(fields["contracts_in"])),
                contracts_out=_split_ids(str(fields["contracts_out"])),
                scope=str(fields["scope"]).strip(),
                acceptance=[str(item) for item in fields["acceptance"]],
                tests=[str(item) for item in fields["tests"]],
                coverage_target=int(coverage.group(1)),
                handoff="" if handoff.lower() in ("none.", "none") else handoff,
            )
        )

    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            finish_task()
            task_id, fields, field = "", {}, ""
            section = line[3:].strip().lower()
            continue
        if section == "contract registry":
            match = re.match(r"^- (\S+?):\s*(.*?)(?:\s+—\s+(.*))?$", line)
            if match and line.strip() != "- none":
                contracts.append(Contract(id=match.group(1), name=match.group(2).strip(), description=(match.group(3) or "").strip()))
        elif section == "tasks":
            heading = _TASK_HEADING.match(line)
            if heading:
                finish_task()
                task_id, fields, field = heading.group(1), {"title": heading.group(2)}, ""
            elif task_id and (match := _FIELD_LINE.match(line)):
                field = _TASK_FIELDS[match.group(1)]
                fields[field] = [] if field in _LIST_FIELDS else match.group(2)
            elif task_id and field in _LIST_FIELDS and (item := re.match(r"^\s+- (.*)$", line)):
                fields[field].append(item.group(1).strip())
            elif task_id and field and field not in _LIST_FIELDS and line.strip():
                fields[field] = f"{fields[field]}\n{line.strip()}"  # a field that runs over several lines
        elif section == "project commands" and (match := _COMMAND_LINE.match(line)):
            commands[match.group(1).lower()] = _parse_command(match.group(2))
    finish_task()

    if not tasks and not issues:
        issues.append("no tasks were found (each task needs a '### T01. Title' heading under '## Tasks')")
    if issues:
        raise PlanParseError(issues)
    return Plan(
        contracts=contracts,
        tasks=tasks,
        commands=ProjectCommands(
            install=commands.get("install"),
            test=commands.get("test") or "",
            lint=commands.get("lint"),
            run=commands.get("run"),
        ),
    )


# ------------------------------------------------------------------- fallback


def _requirement_order(requirements: dict[str, str]) -> list[str]:
    return sorted(requirements, key=lambda r: int(r[1:]))


def fallback_plan(strategy: Mapping[str, Any] | None, prd_markdown: str) -> Plan:
    """A small deterministic plan that validates: one task per workstream (the first sets the project
    up, the rest depend on it), sharing one contract, with the PRD's requirements spread across them."""
    names = workstream_names(strategy)
    requirement_ids = _requirement_order(parse_requirements(prd_markdown)) or ["R1"]
    tasks: list[PlanTask] = []
    for index, name in enumerate(names):
        first = index == 0
        mine = requirement_ids[index :: len(names)] or [requirement_ids[0]]
        tasks.append(
            PlanTask(
                id=f"T{index + 1:02d}",
                title=f"Project setup and {name}" if first else f"Build {name}",
                goal="Establish the MVP baseline and deliver this workstream's first slice."
                if first
                else f"Deliver the {name} workstream.",
                workstream=name,
                depends_on=[] if first else ["T01"],
                requirement_ids=mine,
                contracts_in=[] if first else ["C1"],
                contracts_out=["C1"] if first else [],
                scope="Code, configuration, and tests for this workstream.",
                acceptance=[f"{', '.join(mine)} demonstrably satisfied."],
                tests=["Unit and integration tests for every acceptance criterion."],
                coverage_target=80,
                handoff="Updated docs and task notes.",
            )
        )
    return Plan(
        contracts=[Contract(id="C1", name="Core contract", description="The interface shared between workstreams.")],
        tasks=tasks,
        commands=ProjectCommands(install=None, test=UNSET_TEST_COMMAND, lint=None, run=None),
    )

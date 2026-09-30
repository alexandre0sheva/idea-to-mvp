import pytest
from plan_helpers import PRD, WORKSTREAMS, make_plan, make_task

from idea_to_mvp.plan import (
    Contract,
    Plan,
    PlanTask,
    ProjectCommands,
    execution_waves,
    fallback_plan,
    parse_requirements,
    render_plan_markdown,
    validate_plan,
)


def issues_of(plan: Plan, *, prd: str = PRD, workstreams: list[str] | None = None) -> list[str]:
    return validate_plan(plan, prd_markdown=prd, workstreams=workstreams or WORKSTREAMS)


def test_a_consistent_plan_has_no_issues() -> None:
    assert issues_of(make_plan()) == []


# ------------------------------------------------------------- validate_plan


def test_duplicate_task_ids_are_reported() -> None:
    plan = make_plan([make_task("T01"), make_task("T01", workstream="web-ui")])
    assert any("duplicate task id" in issue and "T01" in issue for issue in issues_of(plan))


def test_malformed_task_ids_are_reported() -> None:
    plan = make_plan([make_task("task-1", requirement_ids=["R1", "R2"]), make_task("T02", workstream="web-ui")])
    assert any("malformed task id" in issue and "task-1" in issue for issue in issues_of(plan))


def test_depends_on_an_unknown_task_is_reported() -> None:
    plan = make_plan([make_task("T01", requirement_ids=["R1", "R2"]), make_task("T02", workstream="web-ui", depends_on=["T09"])])
    assert any("T02" in issue and "unknown task" in issue and "T09" in issue for issue in issues_of(plan))


def test_a_three_node_cycle_is_reported_with_its_path() -> None:
    plan = make_plan(
        [
            make_task("T01", depends_on=["T03"], requirement_ids=["R1", "R2"]),
            make_task("T02", workstream="web-ui", depends_on=["T01"]),
            make_task("T03", depends_on=["T02"]),
        ]
    )
    cycle = [issue for issue in issues_of(plan) if "cycle" in issue]
    assert len(cycle) == 1 and all(task in cycle[0] for task in ("T01", "T02", "T03"))


def test_a_task_depending_on_itself_is_a_cycle() -> None:
    plan = make_plan([make_task("T01", depends_on=["T01"], requirement_ids=["R1", "R2"]), make_task("T02", workstream="web-ui")])
    assert any("cycle" in issue for issue in issues_of(plan))


def test_unknown_workstream_is_reported() -> None:
    plan = make_plan([make_task("T01", requirement_ids=["R1", "R2"]), make_task("T02", workstream="mobile")])
    issues = issues_of(plan)
    assert any("T02" in issue and "unknown workstream" in issue and "mobile" in issue for issue in issues)


def test_a_workstream_without_tasks_is_reported() -> None:
    plan = make_plan([make_task("T01", requirement_ids=["R1", "R2"])])
    assert any("web-ui" in issue and "no tasks" in issue for issue in issues_of(plan))


def test_unknown_contract_ids_are_reported_for_inputs_and_outputs() -> None:
    plan = make_plan(
        [
            make_task("T01", requirement_ids=["R1", "R2"], contracts_out=["C7"]),
            make_task("T02", workstream="web-ui", contracts_in=["C8"]),
        ]
    )
    issues = issues_of(plan)
    assert any("T01" in issue and "unknown contract" in issue and "C7" in issue for issue in issues)
    assert any("T02" in issue and "unknown contract" in issue and "C8" in issue for issue in issues)


def test_requirement_ids_missing_from_the_prd_are_reported() -> None:
    plan = make_plan([make_task("T01", requirement_ids=["R1", "R2", "R9"]), make_task("T02", workstream="web-ui")])
    assert any("T01" in issue and "R9" in issue and "not in the PRD" in issue for issue in issues_of(plan))


def test_a_p0_requirement_no_task_covers_is_reported_but_p1_is_not() -> None:
    plan = make_plan([make_task("T01", requirement_ids=["R1"]), make_task("T02", workstream="web-ui", requirement_ids=["R1"])])
    issues = issues_of(plan)
    assert any("R2" in issue and "P0" in issue and "no task" in issue for issue in issues)
    assert not any("R3" in issue for issue in issues)  # P1


def test_an_empty_test_command_is_reported() -> None:
    plan = make_plan(commands=ProjectCommands(install=None, test="  ", lint=None, run=None))
    assert any("test command" in issue for issue in issues_of(plan))


def test_a_plan_without_tasks_is_reported() -> None:
    assert any("no tasks" in issue for issue in issues_of(make_plan([])))


def test_a_plan_can_have_several_issues_at_once() -> None:
    plan = make_plan([make_task("T01", workstream="mobile", depends_on=["T05"], contracts_in=["C9"])])
    assert len(issues_of(plan)) >= 4


# -------------------------------------------------------------- PRD parsing


def test_requirements_and_priorities_are_read_from_common_prd_layouts() -> None:
    prd = (
        "- R1 (P0): bullet\n**R2** (P1) bold\n| R3 | table row | P0 |\nR4: no priority\n"
        "R5 - P0 dash style\nSee R1 for details.\n"
    )
    assert parse_requirements(prd) == {"R1": "P0", "R2": "P1", "R3": "P0", "R4": "", "R5": "P0"}


def test_a_later_priority_does_not_override_the_first_one() -> None:
    assert parse_requirements("- R1 (P1): a\n- R1 (P0): b\n") == {"R1": "P1"}


# ------------------------------------------------------------ execution_waves


def test_execution_waves_are_topological_layers() -> None:
    diamond = make_plan(
        [
            make_task("T01"),
            make_task("T02", depends_on=["T01"]),
            make_task("T03", depends_on=["T01"]),
            make_task("T04", depends_on=["T02", "T03"]),
        ]
    )
    assert execution_waves(diamond) == [["T01"], ["T02", "T03"], ["T04"]]


def test_independent_tasks_share_a_wave_and_ids_are_sorted() -> None:
    plan = make_plan([make_task("T03"), make_task("T01"), make_task("T02", depends_on=["T03"])])
    assert execution_waves(plan) == [["T01", "T03"], ["T02"]]


def test_execution_waves_reject_a_cycle() -> None:
    plan = make_plan([make_task("T01", depends_on=["T02"]), make_task("T02", depends_on=["T01"])])
    with pytest.raises(ValueError, match="cycle"):
        execution_waves(plan)


# ----------------------------------------------------------------- rendering

EXPECTED_MARKDOWN = """\
## Contract Registry

- C1: Core API — The shared entry contract.

## Tasks

### T01. Project setup
- Goal: Establish the baseline.
- Workstream: backend-api.
- Depends on: none.
- Requirements: R1, R2.
- Contracts in: none.
- Contracts out: C1.
- Implementation scope: Scaffold, tooling, CI.
- Acceptance criteria:
  - Install and boot work.
  - Tests pass.
- Required unit/integration tests:
  - Smoke test of startup.
- Coverage target: at least 80% for changed scope.
- Handoff artifacts: Setup notes.

### T02. Logging endpoint
- Goal: Log a climb.
- Workstream: web-ui.
- Depends on: T01.
- Requirements: R1.
- Contracts in: C1.
- Contracts out: none.
- Implementation scope: Form and API call.
- Acceptance criteria:
  - A climb can be logged.
- Required unit/integration tests:
  - Form submission test.
- Coverage target: at least 90% for changed scope.
- Handoff artifacts: none.

## Project Commands

- Install: `pip install -e .`
- Test: `pytest`
- Lint: none
- Run: `python -m app`
"""


def test_render_plan_markdown_snapshot() -> None:
    plan = Plan(
        contracts=[Contract(id="C1", name="Core API", description="The shared entry contract.")],
        tasks=[
            PlanTask(
                id="T01",
                title="Project setup",
                goal="Establish the baseline.",
                workstream="backend-api",
                depends_on=[],
                requirement_ids=["R1", "R2"],
                contracts_in=[],
                contracts_out=["C1"],
                scope="Scaffold, tooling, CI.",
                acceptance=["Install and boot work.", "Tests pass."],
                tests=["Smoke test of startup."],
                coverage_target=80,
                handoff="Setup notes.",
            ),
            PlanTask(
                id="T02",
                title="Logging endpoint",
                goal="Log a climb.",
                workstream="web-ui",
                depends_on=["T01"],
                requirement_ids=["R1"],
                contracts_in=["C1"],
                contracts_out=[],
                scope="Form and API call.",
                acceptance=["A climb can be logged."],
                tests=["Form submission test."],
                coverage_target=90,
                handoff="",
            ),
        ],
        commands=ProjectCommands(install="pip install -e .", test="pytest", lint=None, run="python -m app"),
    )
    assert render_plan_markdown(plan) == EXPECTED_MARKDOWN


# -------------------------------------------------------------- schema + fallback


def test_the_plan_schema_is_provider_safe_every_field_is_required() -> None:
    schema = Plan.model_json_schema()
    for name, definition in schema["$defs"].items():
        assert set(definition["properties"]) == set(definition["required"]), name
    assert set(schema["properties"]) == set(schema["required"])


def test_the_fallback_plan_is_valid_and_covers_every_requirement() -> None:
    prd = "- R1 (P0): a\n- R2 (P0): b\n- R3 (P0): c\n- R4 (P1): d\n- R5 (P0): e\n"
    strategy = {"workstreams": [{"name": "backend-api"}, {"name": "web-ui"}]}
    plan = fallback_plan(strategy, prd)
    assert validate_plan(plan, prd_markdown=prd, workstreams=["backend-api", "web-ui"]) == []
    assert {t.workstream for t in plan.tasks} == {"backend-api", "web-ui"}
    assert execution_waves(plan)[0] == ["T01"]


def test_the_fallback_plan_without_workstreams_or_requirements_still_validates() -> None:
    plan = fallback_plan(None, "")
    assert validate_plan(plan, prd_markdown="- R1: core\n", workstreams=["core-product"]) == []
    assert len(plan.tasks) == 1 and plan.tasks[0].requirement_ids == ["R1"]


# ----------------------------------------------------- plan.md -> Plan (editing)


def test_a_rendered_plan_parses_back_to_the_same_plan() -> None:
    from idea_to_mvp.plan import parse_plan_markdown, render_plan_markdown

    plan = make_plan(
        [
            make_task("T01", requirement_ids=["R1", "R2"], contracts_out=["C1"], acceptance=["It works.", "It is fast."], tests=["A", "B"]),
            make_task("T02", workstream="web-ui", depends_on=["T01"], contracts_in=["C1"], handoff=""),
            make_task("I2-01", depends_on=["T02"], requirement_ids=[], coverage_target=90),
        ]
    )
    assert parse_plan_markdown(render_plan_markdown(plan)) == plan


def test_missing_commands_and_contracts_round_trip_as_none() -> None:
    from idea_to_mvp.plan import ProjectCommands, parse_plan_markdown, render_plan_markdown

    plan = make_plan(contracts=[], commands=ProjectCommands(install=None, test="pytest -q", lint=None, run=None))
    parsed = parse_plan_markdown(render_plan_markdown(plan))
    assert parsed.contracts == [] and parsed.commands == plan.commands


def test_a_field_may_run_over_several_lines() -> None:
    from idea_to_mvp.plan import parse_plan_markdown, render_plan_markdown

    text = render_plan_markdown(make_plan()).replace("- Goal: Deliver a slice.", "- Goal: Deliver a slice.\n  It spans two lines.", 1)
    assert "It spans two lines." in parse_plan_markdown(text).tasks[0].goal


def test_a_hand_edit_that_changes_a_dependency_is_reflected_in_the_plan() -> None:
    from idea_to_mvp.plan import parse_plan_markdown, render_plan_markdown

    text = render_plan_markdown(make_plan())
    edited = text.replace("### T02. Task T02\n- Goal: Deliver a slice.\n- Workstream: web-ui.\n- Depends on: T01.", "### T02. Task T02\n- Goal: Deliver a slice.\n- Workstream: web-ui.\n- Depends on: none.")
    assert edited != text and parse_plan_markdown(edited).tasks[1].depends_on == []


def test_a_plan_that_cannot_be_read_says_what_is_wrong() -> None:
    from idea_to_mvp.plan import PlanParseError, parse_plan_markdown, render_plan_markdown

    text = render_plan_markdown(make_plan())
    with pytest.raises(PlanParseError) as caught:
        parse_plan_markdown(text.replace("- Goal: Deliver a slice.\n", "", 1))
    assert any("T01" in issue and "Goal" in issue for issue in caught.value.issues)
    with pytest.raises(PlanParseError) as caught:
        parse_plan_markdown("just some prose")
    assert any("no tasks" in issue for issue in caught.value.issues)
    with pytest.raises(PlanParseError) as caught:
        parse_plan_markdown(text.replace("- Coverage target: at least 80% for changed scope.", "- Coverage target: plenty.", 1))
    assert any("Coverage target" in issue for issue in caught.value.issues)


def test_a_plan_without_a_test_command_parses_but_is_flagged_by_the_validator() -> None:
    from idea_to_mvp.plan import parse_plan_markdown, render_plan_markdown

    text = render_plan_markdown(make_plan()).replace("- Test: `pytest`", "- Test: none")
    plan = parse_plan_markdown(text)
    assert plan.commands.test == "" and "the project test command is empty" in validate_plan(plan, prd_markdown=PRD, workstreams=WORKSTREAMS)

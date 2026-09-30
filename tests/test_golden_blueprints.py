"""Golden ideas through the whole blueprint pipeline (offline, demo fixtures).

A regression net for the pack's contract: whatever the idea, the pipeline must end at the implement gate with
a plan that validates, covers every P0 requirement, and can be built in parallel waves.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver

from idea_to_mvp.graph import build_graph, pending_interrupt, run_config
from idea_to_mvp.plan import (
    Plan,
    execution_waves,
    load_plan,
    parse_requirements,
    validate_plan,
    workstream_names,
)
from idea_to_mvp.schemas import ProjectPreferences
from idea_to_mvp.state import make_initial_state

GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "golden_ideas.json").read_text(encoding="utf-8"))
PACK_FILES = ("PRD.md", "ARCHITECTURE.md", "plan.md", "plan.json")


def test_there_are_at_least_three_golden_ideas_with_distinct_ids() -> None:
    assert len(GOLDEN) >= 3 and len({g["id"] for g in GOLDEN}) == len(GOLDEN)
    assert all(g["idea"].strip() and isinstance(g["preferences"], dict) for g in GOLDEN)
    for golden in GOLDEN:
        ProjectPreferences(**golden["preferences"])  # the preferences are valid input


@pytest.fixture(params=GOLDEN, ids=[g["id"] for g in GOLDEN])
def pack(request, demo_env: Path) -> dict[str, Any]:
    """Run the pipeline in autopilot up to the implement gate and return the state and the bundle folder."""
    golden = request.param
    graph = build_graph(MemorySaver())
    config = run_config(f"golden-{golden['id']}")
    state = make_initial_state(
        golden["idea"], 1, autopilot=True, preferences=ProjectPreferences(**golden["preferences"])
    )
    graph.invoke(state, config)
    snapshot = graph.get_state(config)
    return {"values": snapshot.values, "interrupt": pending_interrupt(snapshot), "golden": golden}


def plan_of(values: dict[str, Any]) -> Plan:
    plan = load_plan(values["blueprint_docs"]["plan.json"])
    assert plan is not None, "plan.json does not parse"
    return plan


def test_autopilot_reaches_the_implement_gate_with_a_written_pack(pack: dict[str, Any]) -> None:
    assert (pack["interrupt"] or {}).get("kind") == "implement_gate"
    bundle = Path(pack["values"]["project_bundle_dir"])
    assert bundle.is_dir() and all((bundle / name).is_file() for name in PACK_FILES)
    written = load_plan((bundle / "plan.json").read_text(encoding="utf-8"))
    assert written is not None and written == plan_of(pack["values"])  # the file is the state's plan


def test_the_plan_passes_validation(pack: dict[str, Any]) -> None:
    values = pack["values"]
    issues = validate_plan(
        plan_of(values),
        prd_markdown=values["blueprint_docs"]["PRD.md"],
        workstreams=workstream_names(values["execution_strategy"]),
    )
    assert issues == []


def test_every_p0_requirement_is_covered_by_a_task(pack: dict[str, Any]) -> None:
    requirements = parse_requirements(pack["values"]["blueprint_docs"]["PRD.md"])
    p0 = {rid for rid, priority in requirements.items() if priority == "P0"}
    assert p0, "the PRD states no P0 requirement"
    covered = {rid for task in plan_of(pack["values"]).tasks for rid in task.requirement_ids}
    assert p0 <= covered, f"uncovered P0 requirements: {sorted(p0 - covered)}"


def test_the_plan_can_be_built_in_at_least_two_waves(pack: dict[str, Any]) -> None:
    waves = execution_waves(plan_of(pack["values"]))
    assert len(waves) >= 2
    assert sum(len(wave) for wave in waves) == len(plan_of(pack["values"]).tasks)


def test_the_critic_left_no_blocker_open(pack: dict[str, Any]) -> None:
    issues = pack["values"]["blueprint_review"]["issues"]
    assert not [issue for issue in issues if issue["severity"] == "blocker"]

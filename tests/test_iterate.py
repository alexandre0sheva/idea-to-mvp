"""The iteration loop: change requests after a delivery become new plan tasks, built by the same engine."""

import json
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import BaseMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from plan_helpers import PRD, WORKSTREAMS, make_plan, make_task

from idea_to_mvp import llm
from idea_to_mvp.config import Settings, clear_settings_cache, get_settings
from idea_to_mvp.graph import build_graph, run_config
from idea_to_mvp.implementation.progress import (
    TaskResult,
    load_progress,
    save_result,
    spent_in_iteration,
)
from idea_to_mvp.implementation.workspace import prepare_workspace
from idea_to_mvp.nodes.implement import usage_records
from idea_to_mvp.nodes.iterate import (
    ChangePlan,
    change_planner_node,
    iterate_gate_node,
    route_after_delivery,
    route_after_iterate_gate,
)
from idea_to_mvp.nodes.verify import remaining_budget
from idea_to_mvp.plan import (
    Plan,
    append_iteration_tasks,
    fallback_change_tasks,
    iteration_of,
    load_plan,
    validate_plan,
)
from idea_to_mvp.state import make_initial_state

IDEA = "A habit tracker for climbing gyms"
FEEDBACK = "Add CSV export of the climb history."


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    clear_settings_cache()
    yield
    clear_settings_cache()


def git(workspace: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=workspace, capture_output=True, text=True, check=True).stdout.strip()


# ----------------------------------------------------------------- plan helpers


def test_task_ids_tell_which_iteration_they_belong_to() -> None:
    assert iteration_of("T01") == 1 and iteration_of("T12") == 1
    assert iteration_of("I2-01") == 2 and iteration_of("I10-03") == 10


def test_iteration_tasks_are_appended_without_touching_the_finished_ones() -> None:
    plan = make_plan()
    extended = append_iteration_tasks(plan, [make_task("I2-01", depends_on=["T02"])])
    assert [t.id for t in extended.tasks] == ["T01", "T02", "I2-01"]
    assert extended.tasks[:2] == plan.tasks and extended.contracts == plan.contracts and extended.commands == plan.commands
    assert [t.id for t in plan.tasks] == ["T01", "T02"]  # the input is not modified


def test_an_iteration_task_may_not_reuse_an_existing_id() -> None:
    with pytest.raises(ValueError, match="T01"):
        append_iteration_tasks(make_plan(), [make_task("T01", title="Rewritten")])


def test_the_plan_validator_accepts_iteration_ids_and_rejects_other_shapes() -> None:
    plan = append_iteration_tasks(make_plan(), [make_task("I2-01", depends_on=["T02"])])
    assert validate_plan(plan, prd_markdown=PRD, workstreams=WORKSTREAMS) == []
    bad = append_iteration_tasks(make_plan(), [make_task("X2-01")])
    assert any("malformed task id 'X2-01'" in issue for issue in validate_plan(bad, prd_markdown=PRD, workstreams=WORKSTREAMS))


def test_the_fallback_change_is_one_valid_task_carrying_the_feedback() -> None:
    plan = make_plan()
    tasks = fallback_change_tasks(plan, 3, FEEDBACK)
    assert [t.id for t in tasks] == ["I3-01"] and FEEDBACK in tasks[0].goal
    assert tasks[0].workstream in WORKSTREAMS
    assert validate_plan(append_iteration_tasks(plan, tasks), prd_markdown=PRD, workstreams=WORKSTREAMS) == []


def _result(task_id: str, cost: float, status: str = "done") -> TaskResult:
    return {"task_id": task_id, "status": status, "summary": "s", "cost_usd": cost, "turns": 1, "session_id": None, "commit": None}  # type: ignore[typeddict-item]


def test_spend_is_counted_per_iteration() -> None:
    progress = {"T01": _result("T01", 1.0), "T02": _result("T02", 2.0), "I2-01": _result("I2-01", 0.5), "I3-01": _result("I3-01", 4.0)}
    assert spent_in_iteration(progress, 1) == 3.0
    assert spent_in_iteration(progress, 2) == 0.5
    assert spent_in_iteration(progress, 4) == 0.0


def test_usage_records_cover_only_the_iteration_that_just_ran() -> None:
    results = [_result("T01", 1.0), _result("I2-01", 0.5), _result("I2-02", 0.0)]
    settings = Settings(_env_file=None)
    assert [r["cost_usd"] for r in usage_records(results, settings, iteration=2)] == [0.5]
    assert [r["cost_usd"] for r in usage_records(results, settings, iteration=1)] == [1.0]
    assert len(usage_records(results, settings)) == 2  # no iteration: every result with a cost


def test_each_iteration_gets_a_fresh_whole_run_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMPLEMENTER_MAX_TOTAL_USD", "25")
    clear_settings_cache()
    settings = get_settings()
    usage: Any = [{"role": "implementer", "provider": "anthropic", "model": "m", "input_tokens": 0, "output_tokens": 0, "cost_usd": 24.0}]
    state: Any = {"usage": usage}
    assert remaining_budget(state, settings) == 1.0
    assert remaining_budget({**state, "spent_before_iteration": 24.0}, settings) == 25.0
    assert remaining_budget({**state, "spent_before_iteration": 20.0}, settings) == 21.0


# ------------------------------------------------------------ change planner


class FakeRuntime:
    system_prompt = "You are the change planner."


class FakeModel:
    """Replaces `llm.invoke_structured`: replies come from a script (None = use the node's fallback)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *replies: ChangePlan | None) -> None:
        self.replies = list(replies)
        self.calls: list[list[BaseMessage]] = []
        monkeypatch.setattr(llm, "get_runtime", lambda role: FakeRuntime())
        monkeypatch.setattr(llm, "invoke_structured", self.invoke)

    def invoke(self, runtime: Any, messages: list[BaseMessage], schema: Any, *, fallback: Any = None) -> Any:
        self.calls.append(messages)
        reply = self.replies.pop(0) if self.replies else None
        return reply if reply is not None else fallback()

    def prompt(self, call: int = 0) -> str:
        return "\n".join(str(m.content) for m in self.calls[call])


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    bundle = tmp_path / "blueprints" / "20260101-000000-000000-idea"
    bundle.mkdir(parents=True)
    plan = make_plan()
    (bundle / "PRD.md").write_text(PRD)
    (bundle / "plan.json").write_text(plan.model_dump_json(indent=2))
    (bundle / "plan.md").write_text("old plan.md")
    ws = prepare_workspace(bundle, tmp_path / "projects")
    save_result(ws, _result("T01", 1.0))
    save_result(ws, _result("T02", 2.0))
    return ws


def planner_state(workspace: Path, *, iteration: int = 2, mode: str = "agent_team", usage: Any = None) -> Any:
    state: dict[str, Any] = dict(make_initial_state(IDEA, 1))
    state.update(
        workspace_dir=str(workspace),
        iteration=iteration,
        change_requests=[FEEDBACK],
        execution_strategy={"mode": mode, "reasoning": "", "workstreams": [{"name": n} for n in WORKSTREAMS]},
        usage=usage or [],
        verification={"passed": True, "attempts": 2, "report": "old", "lanes": [{"lane": "tests"}]},
    )
    return state


def good_change(task_id: str = "I2-01") -> ChangePlan:
    return ChangePlan(tasks=[make_task(task_id, title="CSV export", goal=FEEDBACK, depends_on=["T02"], requirement_ids=[])])


def test_the_planner_appends_the_new_tasks_to_plan_json_and_plan_md(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeModel(monkeypatch, good_change())
    before = load_plan((workspace / "plan.json").read_text())
    update = change_planner_node(planner_state(workspace))
    plan = load_plan((workspace / "plan.json").read_text())
    assert plan is not None and before is not None
    assert [t.id for t in plan.tasks] == ["T01", "T02", "I2-01"] and plan.tasks[:2] == before.tasks
    assert "### I2-01. CSV export" in (workspace / "plan.md").read_text() and "### T01." in (workspace / "plan.md").read_text()
    assert update["stage"] == "implementation"


def test_the_new_plan_is_committed_so_task_worktrees_see_it(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeModel(monkeypatch, good_change())
    change_planner_node(planner_state(workspace))
    assert git(workspace, "log", "-1", "--format=%s") == "I2: plan"
    assert "I2-01" in git(workspace, "show", "HEAD:plan.json")


def test_the_prompt_names_the_feedback_the_id_prefix_and_the_existing_tasks(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    model = FakeModel(monkeypatch, good_change())
    change_planner_node(planner_state(workspace))
    prompt = model.prompt()
    assert FEEDBACK in prompt and "I2-01" in prompt and "T01" in prompt and "T02" in prompt
    assert "backend-api" in prompt and "web-ui" in prompt and "R1" in prompt  # workstreams and PRD requirements


def test_a_bad_plan_gets_one_repair_with_the_problems_listed(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    broken = ChangePlan(tasks=[make_task("I2-01", depends_on=["T99"])])
    model = FakeModel(monkeypatch, broken, good_change())
    change_planner_node(planner_state(workspace))
    assert len(model.calls) == 2 and "T99" in model.prompt(1) and "unknown task" in model.prompt(1)
    plan = load_plan((workspace / "plan.json").read_text())
    assert plan is not None and plan.tasks[-1].title == "CSV export"


def test_a_plan_that_stays_invalid_is_replaced_by_the_deterministic_fallback(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    broken = ChangePlan(tasks=[make_task("T05")])  # not an iteration id
    model = FakeModel(monkeypatch, broken, broken)
    change_planner_node(planner_state(workspace))
    assert len(model.calls) == 2
    plan = load_plan((workspace / "plan.json").read_text())
    assert plan is not None and [t.id for t in plan.tasks] == ["T01", "T02", "I2-01"]
    assert FEEDBACK in plan.tasks[-1].goal
    assert validate_plan(plan, prd_markdown=PRD, workstreams=WORKSTREAMS) == []


@pytest.mark.parametrize(
    "tasks",
    [
        [],  # nothing planned
        [make_task("T01", title="Rewritten")],  # redefines a finished task
        [make_task("I3-01")],  # another iteration's id
        [make_task("I2-02")],  # ids must start at 01
    ],
)
def test_change_plans_that_do_not_extend_the_plan_are_rejected(workspace: Path, monkeypatch: pytest.MonkeyPatch, tasks: list) -> None:
    model = FakeModel(monkeypatch, ChangePlan(tasks=tasks), ChangePlan(tasks=tasks))
    change_planner_node(planner_state(workspace))
    assert len(model.calls) == 2  # rejected, repaired once, still rejected
    plan = load_plan((workspace / "plan.json").read_text())
    assert plan is not None and [t.id for t in plan.tasks] == ["T01", "T02", "I2-01"] and plan.tasks[0].title == "Task T01"


def test_problems_the_plan_already_had_are_not_held_against_the_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = tmp_path / "blueprints" / "20260101-000000-000000-idea"
    bundle.mkdir(parents=True)
    plan = make_plan([make_task("T01", requirement_ids=["R1"])])  # R2 (P0) is covered by no task, and web-ui has none
    (bundle / "PRD.md").write_text(PRD)
    (bundle / "plan.json").write_text(plan.model_dump_json())
    ws = prepare_workspace(bundle, tmp_path / "projects")
    save_result(ws, _result("T01", 1.0))
    model = FakeModel(monkeypatch, ChangePlan(tasks=[make_task("I2-01", title="CSV export", depends_on=["T01"])]))
    change_planner_node(planner_state(ws))
    assert len(model.calls) == 1  # accepted at once
    stored = load_plan((ws / "plan.json").read_text())
    assert stored is not None and stored.tasks[-1].title == "CSV export"


def test_a_new_iteration_starts_verification_and_the_budget_from_scratch(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeModel(monkeypatch, good_change())
    usage = [{"role": "implementer", "provider": "anthropic", "model": "m", "input_tokens": 0, "output_tokens": 0, "cost_usd": 3.0}]
    update = change_planner_node(planner_state(workspace, usage=usage))
    assert update["verification"] == {"passed": False, "attempts": 0, "report": "", "lanes": []}
    assert update["spent_before_iteration"] == 3.0


def test_tasks_a_lead_session_built_count_as_done_so_they_are_not_built_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = tmp_path / "blueprints" / "20260101-000000-000000-idea"
    bundle.mkdir(parents=True)
    (bundle / "PRD.md").write_text(PRD)
    (bundle / "plan.json").write_text(make_plan().model_dump_json())
    ws = prepare_workspace(bundle, tmp_path / "projects")  # strategy `subagents`: no progress was recorded
    FakeModel(monkeypatch, good_change())
    change_planner_node(planner_state(ws, mode="subagents"))
    progress = load_progress(ws)
    assert progress["T01"]["status"] == "done" and progress["T02"]["status"] == "done" and "I2-01" not in progress


def test_a_missing_plan_is_reported_clearly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeModel(monkeypatch, good_change())
    empty = tmp_path / "ws"
    empty.mkdir()
    with pytest.raises(RuntimeError, match="plan.json"):
        change_planner_node(planner_state(empty))


# ------------------------------------------------------------------ the gate


def gate_state(**fields: Any) -> Any:
    state: dict[str, Any] = dict(make_initial_state(IDEA, 1))
    state.update(delivery_report="## Delivery report", iteration=1, verification={"passed": True, "attempts": 0, "report": "", "lanes": []})
    state.update(fields)
    return state


def test_the_gate_is_not_offered_once_the_iteration_limit_is_reached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_ITERATIONS", "3")
    clear_settings_cache()
    assert route_after_delivery(gate_state(iteration=1)) == "iterate_gate"
    assert route_after_delivery(gate_state(iteration=2)) == "iterate_gate"
    assert route_after_delivery(gate_state(iteration=3)) == "__end__"


def test_the_gate_routes_on_the_decision() -> None:
    assert route_after_iterate_gate(gate_state(iterate_decision={"iterate": True, "feedback": "x"})) == "change_planner"
    assert route_after_iterate_gate(gate_state(iterate_decision={"iterate": False, "feedback": ""})) == "__end__"


# ----------------------------------------------------------- in the real graph


@pytest.fixture()
def demo(demo_env: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MAX_ITERATIONS", "5")
    clear_settings_cache()
    return demo_env


async def to_first_delivery(graph: Any, config: Any) -> list[Any]:
    await graph.ainvoke(make_initial_state(IDEA, 1), config)
    await graph.ainvoke(Command(resume="1. Solo climbers."), config)
    await graph.ainvoke(Command(resume={"option": "A", "notes": ""}), config)
    await graph.ainvoke(Command(resume={"generate": True, "notes": ""}), config)
    await graph.ainvoke(Command(resume={"implement": True, "notes": ""}), config)
    return await pending_gates(graph, config)


async def pending_gates(graph: Any, config: Any) -> list[Any]:
    snapshot = await graph.aget_state(config)
    return [i for task in snapshot.tasks for i in task.interrupts]


async def iterate(graph: Any, config: Any, feedback: str = FEEDBACK) -> tuple[list[dict[str, Any]], list[Any]]:
    events: list[dict[str, Any]] = []
    async for _namespace, mode, payload in graph.astream(  # subgraphs=True: the parallel engine is a subgraph
        Command(resume={"iterate": True, "feedback": feedback}), config, stream_mode=["updates", "custom"], subgraphs=True
    ):
        if mode == "custom":
            events.append(payload)
    return events, await pending_gates(graph, config)


async def test_the_first_delivery_is_tagged_zipped_and_offers_to_iterate(demo: Path) -> None:
    graph = build_graph(MemorySaver())
    config = run_config("iter-first")
    (gate,) = await to_first_delivery(graph, config)
    assert gate.value["kind"] == "iterate_gate" and gate.value["iteration"] == 1
    assert "Delivery report" in gate.value["report"]
    values = (await graph.aget_state(config)).values
    workspace = Path(values["workspace_dir"])
    assert git(workspace, "tag", "--list") == "v0.1"
    archive = Path(values["delivery_zip"])
    assert archive.is_relative_to(demo / "deliveries") and archive.name.endswith("-v0.1.zip") and archive.exists()
    assert "v0.1" in values["delivery_report"] and archive.name in values["delivery_report"]
    assert values["iteration"] == 1 and values["change_requests"] == []


async def test_declining_to_iterate_ends_the_graph(demo: Path) -> None:
    graph = build_graph(MemorySaver())
    config = run_config("iter-decline")
    await to_first_delivery(graph, config)
    await graph.ainvoke(Command(resume={"iterate": False, "feedback": ""}), config)
    snapshot = await graph.aget_state(config)
    assert snapshot.next == () and snapshot.values["iteration"] == 1 and snapshot.values["stage"] == "done"


async def test_asking_for_changes_without_saying_which_ends_the_graph(demo: Path) -> None:
    graph = build_graph(MemorySaver())
    config = run_config("iter-empty")
    await to_first_delivery(graph, config)
    await graph.ainvoke(Command(resume={"iterate": True, "feedback": "   "}), config)
    assert (await graph.aget_state(config)).next == ()


async def test_feedback_becomes_new_tasks_a_passing_verification_tag_v0_2_and_a_new_zip(demo: Path) -> None:
    graph = build_graph(MemorySaver())
    config = run_config("iter-second")
    await to_first_delivery(graph, config)
    first_zip = Path((await graph.aget_state(config)).values["delivery_zip"])
    events, gates = await iterate(graph, config)

    values = (await graph.aget_state(config)).values
    workspace = Path(values["workspace_dir"])
    assert values["iteration"] == 2 and values["change_requests"] == [FEEDBACK]
    plan = load_plan((workspace / "plan.json").read_text())
    assert plan is not None and [t.id for t in plan.tasks] == ["T01", "T02", "T03", "I2-01"]
    assert list(values["task_results"]) == ["T01", "T02", "T03", "I2-01"]
    assert values["task_results"]["I2-01"]["status"] == "done" and (workspace / "notes" / "I2-01.md").exists()
    assert values["verification"]["passed"] is True and values["verification"]["attempts"] == 0
    assert set(git(workspace, "tag", "--list").split()) == {"v0.1", "v0.2"}
    assert git(workspace, "rev-list", "-n1", "v0.2") == git(workspace, "rev-parse", "HEAD")
    assert "I2-01: " in git(workspace, "log", "--format=%s")
    second_zip = Path(values["delivery_zip"])
    assert second_zip != first_zip and second_zip.name.endswith("-v0.2.zip") and first_zip.exists()
    with zipfile.ZipFile(second_zip) as bundle:
        assert f"{workspace.name}/notes/I2-01.md" in bundle.namelist()
    (gate,) = gates
    assert gate.value["kind"] == "iterate_gate" and gate.value["iteration"] == 2
    started = [e["task_id"] for e in events if e["kind"] == "task_start" and not str(e["task_id"]).startswith("verify:")]
    assert started == ["I2-01"]  # only the new task ran; the finished ones were skipped


async def test_the_sequential_engine_also_runs_only_the_new_tasks(demo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMPLEMENTER_MAX_PARALLEL", "1")
    clear_settings_cache()
    graph = build_graph(MemorySaver())
    config = run_config("iter-sequential")
    await to_first_delivery(graph, config)
    events, _ = await iterate(graph, config)
    started = [e["task_id"] for e in events if e["kind"] == "task_start" and not str(e["task_id"]).startswith("verify:")]
    assert started == ["I2-01"]
    values = (await graph.aget_state(config)).values
    assert values["task_results"]["I2-01"]["status"] == "done" and values["verification"]["passed"] is True


async def test_no_further_gate_is_offered_after_max_iterations(demo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_ITERATIONS", "2")
    clear_settings_cache()
    graph = build_graph(MemorySaver())
    config = run_config("iter-limit")
    assert len(await to_first_delivery(graph, config)) == 1  # v0.1 may still be iterated on
    _, gates = await iterate(graph, config)
    snapshot = await graph.aget_state(config)
    assert gates == [] and snapshot.next == () and snapshot.values["iteration"] == 2
    assert "v0.2" in git(Path(snapshot.values["workspace_dir"]), "tag", "--list")


async def test_a_single_allowed_iteration_ends_after_the_first_delivery(demo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_ITERATIONS", "1")
    clear_settings_cache()
    graph = build_graph(MemorySaver())
    config = run_config("iter-one")
    assert await to_first_delivery(graph, config) == []
    assert (await graph.aget_state(config)).next == ()


async def test_two_rounds_of_feedback_stack_up_in_the_same_workspace(demo: Path) -> None:
    graph = build_graph(MemorySaver())
    config = run_config("iter-twice")
    await to_first_delivery(graph, config)
    await iterate(graph, config, "First change.")
    _, gates = await iterate(graph, config, "Second change.")
    values = (await graph.aget_state(config)).values
    workspace = Path(values["workspace_dir"])
    assert values["iteration"] == 3 and values["change_requests"] == ["First change.", "Second change."]
    plan = load_plan((workspace / "plan.json").read_text())
    assert plan is not None and [t.id for t in plan.tasks][-2:] == ["I2-01", "I3-01"]
    assert set(git(workspace, "tag", "--list").split()) == {"v0.1", "v0.2", "v0.3"}
    assert gates[0].value["iteration"] == 3
    assert values["verification"]["attempts"] == 0
    assert json.loads((workspace / ".idea-to-mvp" / "progress.json").read_text()).keys() >= {"T01", "I2-01", "I3-01"}


async def test_iterating_a_lead_session_project_builds_only_the_new_tasks(demo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Strategy `subagents` builds v0.1 in one session (no per-task progress); iterations still go task by task."""
    from idea_to_mvp.demo import fixtures

    original = fixtures.execution_strategy

    def lead_session_strategy(messages: list[BaseMessage]) -> Any:
        return original(messages).model_copy(update={"mode": "subagents"})

    monkeypatch.setitem(fixtures.STRUCTURED, type(original([])), lead_session_strategy)
    graph = build_graph(MemorySaver())
    config = run_config("iter-lead")
    await to_first_delivery(graph, config)
    assert (await graph.aget_state(config)).values["task_results"] == {}
    events, _ = await iterate(graph, config)
    values = (await graph.aget_state(config)).values
    started = [e["task_id"] for e in events if e["kind"] == "task_start" and not str(e["task_id"]).startswith("verify:")]
    assert started == ["I2-01"] and values["task_results"]["I2-01"]["status"] == "done"
    assert Plan.model_validate_json((Path(values["workspace_dir"]) / "plan.json").read_text()).tasks[-1].id == "I2-01"


def test_the_gate_node_carries_the_report_and_the_iteration(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_interrupt(payload: dict[str, Any]) -> Any:
        seen.update(payload)
        return {"iterate": True, "feedback": FEEDBACK}

    monkeypatch.setattr("idea_to_mvp.nodes.iterate.interrupt", fake_interrupt)
    update = iterate_gate_node(gate_state(iteration=2, change_requests=["earlier"]))
    assert seen["kind"] == "iterate_gate" and seen["iteration"] == 2 and seen["report"] == "## Delivery report"
    assert update["iterate_decision"] == {"iterate": True, "feedback": FEEDBACK}
    assert update["iteration"] == 3 and update["change_requests"] == ["earlier", FEEDBACK] and update["stage"] == "implementation"

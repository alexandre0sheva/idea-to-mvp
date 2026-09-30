"""Parallel implementation in the real graph (demo mode): routing, worktrees, events, resume."""

import subprocess
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from plan_helpers import make_plan, make_task

from idea_to_mvp.config import clear_settings_cache
from idea_to_mvp.graph import build_graph, run_config
from idea_to_mvp.implementation import executor
from idea_to_mvp.implementation.progress import load_progress
from idea_to_mvp.nodes.implement import route_after_workspace
from idea_to_mvp.state import make_initial_state

IDEA = "A habit tracker for climbing gyms"


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def use_parallel(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("IMPLEMENTER_MAX_PARALLEL", value)
    clear_settings_cache()


async def to_implement_gate(graph: Any, config: Any) -> None:
    await graph.ainvoke(make_initial_state(IDEA, 1), config)
    await graph.ainvoke(Command(resume="1. Solo climbers."), config)
    await graph.ainvoke(Command(resume={"option": "A", "notes": ""}), config)
    await graph.ainvoke(Command(resume={"generate": True, "notes": ""}), config)


def git_log(workspace: Path, *args: str) -> list[str]:
    return subprocess.run(
        ["git", "log", *args, "--format=%s"], cwd=workspace, capture_output=True, text=True, check=True
    ).stdout.splitlines()


async def test_the_demo_plan_runs_its_independent_tasks_in_parallel_and_merges_them(
    demo_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    use_parallel(monkeypatch, "3")
    graph = build_graph(MemorySaver())
    config = run_config("parallel-demo")
    await to_implement_gate(graph, config)
    custom: list[dict[str, Any]] = []
    async for _namespace, mode, payload in graph.astream(  # subgraphs=True, as SubmitService does
        Command(resume={"implement": True, "notes": ""}), config, stream_mode=["updates", "custom"], subgraphs=True
    ):
        if mode == "custom":
            custom.append(payload)

    values = (await graph.aget_state(config)).values
    workspace = Path(values["workspace_dir"])
    assert [r["status"] for r in values["task_results"].values()] == ["done"] * 3
    assert {"T01: Project setup", "T02: Core journey", "T03: Progress view"} <= set(git_log(workspace))
    assert values["verification"]["passed"] is True and values["stage"] == "done"
    assert not (demo_env / "projects" / ".worktrees").exists()  # every worktree was cleaned up
    order = [(e["kind"], e["task_id"]) for e in custom if e["kind"] in ("task_start", "task_end") and e["task_id"]]
    assert order.index(("task_start", "T03")) < order.index(("task_end", "T02"))  # two sessions overlapped in the events
    assert load_progress(workspace).keys() == {"T01", "T02", "T03"}


async def test_max_parallel_one_reproduces_the_sequential_engine(demo_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_parallel(monkeypatch, "1")
    graph = build_graph(MemorySaver())
    config = run_config("parallel-one")
    await to_implement_gate(graph, config)
    await graph.ainvoke(Command(resume={"implement": True, "notes": ""}), config)
    workspace = Path((await graph.aget_state(config)).values["workspace_dir"])
    assert git_log(workspace)[:3] == ["T03: Progress view", "T02: Core journey", "T01: Project setup"]  # linear, no merges
    assert git_log(workspace, "--merges") == []
    assert not (demo_env / "projects" / ".worktrees").exists()


# ------------------------------------------------------------------------ routing


def routed_state(tmp_path: Path, plan: Any = None, *, git: bool = True, mode: str = "agent_team") -> dict[str, Any]:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    if git:
        (workspace / ".git").mkdir()
    if plan is not None:
        (workspace / "plan.json").write_text(plan.model_dump_json())
    state: dict[str, Any] = dict(make_initial_state(IDEA, 1))
    state.update(workspace_dir=str(workspace), execution_strategy={"mode": mode, "reasoning": "", "workstreams": []})
    return state


@pytest.fixture()
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "out"))
    use_parallel(monkeypatch, "3")


def test_a_runnable_plan_in_a_git_workspace_goes_to_the_parallel_subgraph(env, tmp_path: Path) -> None:
    assert route_after_workspace(routed_state(tmp_path, make_plan())) == "implement_plan"  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "case",
    ["parallel_off", "lead_mode", "no_plan", "no_git", "cyclic_plan"],
)
def test_everything_else_keeps_the_single_node_implementer(env, tmp_path: Path, monkeypatch, case: str) -> None:
    cyclic = make_plan([make_task("T01", depends_on=["T02"]), make_task("T02", depends_on=["T01"])])
    states = {
        "parallel_off": lambda: routed_state(tmp_path, make_plan()),
        "lead_mode": lambda: routed_state(tmp_path, make_plan(), mode="subagents"),
        "no_plan": lambda: routed_state(tmp_path),
        "no_git": lambda: routed_state(tmp_path, make_plan(), git=False),
        "cyclic_plan": lambda: routed_state(tmp_path, cyclic),
    }
    if case == "parallel_off":
        use_parallel(monkeypatch, "1")
    assert route_after_workspace(states[case]()) == "implementer"  # type: ignore[arg-type]


# ------------------------------------------------------------------------- resume


async def test_a_killed_parallel_run_resumes_in_the_same_workspace(demo_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_parallel(monkeypatch, "3")
    graph = build_graph(MemorySaver())
    config = run_config("parallel-resume")
    await to_implement_gate(graph, config)

    real_run_task = executor.run_task
    started: list[str] = []

    async def dies_on_t03(workspace: Path, task: Any, *args: Any, **kwargs: Any) -> Any:
        started.append(task.id)
        if task.id == "T03" and started.count("T03") == 1:
            raise RuntimeError("the app was killed")
        return await real_run_task(workspace, task, *args, **kwargs)

    monkeypatch.setattr(executor, "run_task", dies_on_t03)
    with pytest.raises(RuntimeError, match="killed"):
        await graph.ainvoke(Command(resume={"implement": True, "notes": ""}), config)
    crashed = await graph.aget_state(config)
    workspace = Path(crashed.values["workspace_dir"])
    assert list(load_progress(workspace)) == ["T01"]  # T02 finished its session but was never merged, so not `done`

    await graph.ainvoke(None, config)
    values = (await graph.aget_state(config)).values
    assert Path(values["workspace_dir"]) == workspace
    assert [d.name for d in (demo_env / "projects").iterdir() if not d.name.startswith(".")] == [workspace.name]
    assert [r["status"] for r in values["task_results"].values()] == ["done"] * 3
    assert git_log(workspace).count("T01: Project setup") == 1 and values["stage"] == "done"


async def test_the_implement_gate_reports_the_plans_width(demo_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_parallel(monkeypatch, "3")
    graph = build_graph(MemorySaver())
    config = run_config("gate-width")
    await to_implement_gate(graph, config)
    snapshot = await graph.aget_state(config)
    gate = snapshot.tasks[0].interrupts[0].value
    assert gate["kind"] == "implement_gate" and gate["task_count"] == 3 and gate["dag_width"] == 2
    assert gate["max_parallel"] == 3 and "2 of them can run in parallel" in gate["question"]


# ------------------------------------------------------- chosen at the implement gate


def test_the_parallelism_picked_at_the_gate_overrides_the_setting_in_both_directions(
    env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = routed_state(tmp_path, make_plan())
    state["implement_decision"] = {"implement": True, "notes": "", "parallel": 1}
    assert route_after_workspace(state) == "implementer"  # Sequential chosen although IMPLEMENTER_MAX_PARALLEL=3
    use_parallel(monkeypatch, "1")
    state["implement_decision"] = {"implement": True, "notes": "", "parallel": 2}
    assert route_after_workspace(state) == "implement_plan"  # Parallel 2 chosen although the setting says 1
    state["implement_decision"] = {"implement": True, "notes": "", "parallel": 0}
    assert route_after_workspace(state) == "implementer"  # no choice: the setting (1) applies


async def test_choosing_sequential_at_the_gate_runs_the_sequential_engine(demo_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_parallel(monkeypatch, "3")
    graph = build_graph(MemorySaver())
    config = run_config("parallel-gate-choice")
    await to_implement_gate(graph, config)
    await graph.ainvoke(Command(resume={"implement": True, "notes": "", "parallel": 1}), config)
    values = (await graph.aget_state(config)).values
    workspace = Path(values["workspace_dir"])
    assert values["implement_decision"]["parallel"] == 1
    assert git_log(workspace, "--merges") == [] and [r["status"] for r in values["task_results"].values()] == ["done"] * 3

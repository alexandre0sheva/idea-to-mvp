"""The implement steps in the real graph: a stable workspace, task-by-task execution, live events, resume."""

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from idea_to_mvp import implementer
from idea_to_mvp.config import clear_settings_cache
from idea_to_mvp.graph import build_graph, run_config
from idea_to_mvp.implementation import executor
from idea_to_mvp.implementation.progress import TaskResult, load_progress, save_result
from idea_to_mvp.nodes import implement as implement_nodes
from idea_to_mvp.state import make_initial_state
from idea_to_mvp.usage import summarize_usage

IDEA = "A habit tracker for climbing gyms"


async def to_implement_gate(graph: Any, config: Any) -> None:
    await graph.ainvoke(make_initial_state(IDEA, 1), config)
    await graph.ainvoke(Command(resume="1. Solo climbers."), config)
    await graph.ainvoke(Command(resume={"option": "A", "notes": ""}), config)
    await graph.ainvoke(Command(resume={"generate": True, "notes": ""}), config)


def implement(graph: Any, config: Any, **kwargs: Any) -> Any:
    return graph.ainvoke(Command(resume={"implement": True, "notes": ""}), config, **kwargs)


def git_log(workspace: Path) -> list[str]:
    return subprocess.run(
        ["git", "log", "--format=%s"], cwd=workspace, capture_output=True, text=True, check=True
    ).stdout.splitlines()


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    # This file covers the sequential engine; parallel execution is covered in test_implement_parallel.py.
    monkeypatch.setenv("IMPLEMENTER_MAX_PARALLEL", "1")
    clear_settings_cache()


# ------------------------------------------------------------------ demo engine


async def test_the_demo_plan_is_implemented_task_by_task(demo_env: Path) -> None:
    graph = build_graph(MemorySaver())
    config = run_config("engine-demo")
    await to_implement_gate(graph, config)
    await implement(graph, config)
    values = (await graph.aget_state(config)).values
    workspace = Path(values["workspace_dir"])
    assert workspace.parent == demo_env / "projects"
    assert [r["status"] for r in values["task_results"].values()] == ["done"] * 3
    assert list(values["task_results"]) == ["T01", "T02", "T03"]
    log = git_log(workspace)
    assert log[:3] == ["T03: Progress view", "T02: Core journey", "T01: Project setup"] and log[-1] == "blueprint"
    assert load_progress(workspace).keys() == values["task_results"].keys()
    assert "## T01 — Project setup: done" in values["implementation_log"]
    assert values["verification"]["passed"] is True and values["stage"] == "done"


async def test_custom_events_stream_while_the_node_runs(demo_env: Path) -> None:
    graph = build_graph(MemorySaver())
    config = run_config("engine-events")
    await to_implement_gate(graph, config)
    custom: list[dict[str, Any]] = []
    async for mode, payload in graph.astream(
        Command(resume={"implement": True, "notes": ""}), config, stream_mode=["updates", "custom"]
    ):
        if mode == "custom":
            custom.append(payload)
    implementation = [e for e in custom if not str(e["task_id"]).startswith("verify:")]
    kinds = [e["kind"] for e in implementation]
    assert kinds.count("task_start") == 3 and kinds.count("task_end") == 3 and "tool" in kinds
    assert [e["task_id"] for e in implementation if e["kind"] == "task_start"] == ["T01", "T02", "T03"]
    # The verification lanes stream their progress too.
    lanes = {e["task_id"] for e in custom if e["kind"] == "task_end" and str(e["task_id"]).startswith("verify:")}
    assert lanes == {"verify:tests", "verify:quality", "verify:requirements"}


async def test_a_killed_run_resumes_at_the_first_unfinished_task_in_the_same_workspace(
    demo_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = build_graph(MemorySaver())
    config = run_config("engine-resume")
    await to_implement_gate(graph, config)

    real_run_task = executor.run_task
    started: list[str] = []

    async def dies_on_t02(workspace: Path, task: Any, *args: Any, **kwargs: Any) -> TaskResult:
        started.append(task.id)
        if task.id == "T02" and started.count("T02") == 1:
            raise RuntimeError("the app was killed")
        return await real_run_task(workspace, task, *args, **kwargs)

    monkeypatch.setattr(executor, "run_task", dies_on_t02)
    with pytest.raises(RuntimeError, match="killed"):
        await implement(graph, config)
    crashed = await graph.aget_state(config)
    workspace = Path(crashed.values["workspace_dir"])  # already known: the workspace step finished and was checkpointed
    assert crashed.next == ("implementer",) and list(load_progress(workspace)) == ["T01"]

    await graph.ainvoke(None, config)  # restart and continue
    values = (await graph.aget_state(config)).values
    assert Path(values["workspace_dir"]) == workspace and len(list((demo_env / "projects").iterdir())) == 1
    assert [r["status"] for r in values["task_results"].values()] == ["done"] * 3
    log = git_log(workspace)
    assert log.count("T01: Project setup") == 1  # T01 was not implemented a second time
    assert values["stage"] == "done"


# ---------------------------------------------------------- modes and fallbacks


async def test_the_lead_session_mode_still_runs_the_whole_plan_in_one_session(
    demo_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_lead(workspace: Path, strategy: dict, settings: Any) -> str:
        calls.append(strategy["mode"])
        (Path(workspace) / "lead.txt").write_text("built by the lead")
        return "Lead built everything."

    monkeypatch.setattr(implementer, "run_implementation", fake_lead)
    graph = build_graph(MemorySaver())
    config = run_config("engine-lead")
    await to_implement_gate(graph, config)
    await graph.aupdate_state(config, {"execution_strategy": {"mode": "subagents", "reasoning": "x", "workstreams": [{"name": "core-app", "focus": "f", "deliverables": "d"}]}})
    await implement(graph, config)
    values = (await graph.aget_state(config)).values
    assert calls == ["subagents"] and values["implementation_log"] == "Lead built everything."
    assert values["task_results"] == {}
    assert git_log(Path(values["workspace_dir"]))[0] == "implementation"  # the orchestrator committed it


async def test_a_workspace_without_a_usable_plan_falls_back_to_the_lead_session(
    demo_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(implementer, "run_implementation", lambda ws, strategy, settings: "Lead fallback.")
    graph = build_graph(MemorySaver())
    config = run_config("engine-noplan")
    await to_implement_gate(graph, config)
    values = (await graph.aget_state(config)).values
    (Path(values["project_bundle_dir"]) / "plan.json").write_text('{"tasks": "broken"}')
    await implement(graph, config)
    final = (await graph.aget_state(config)).values
    assert final["implementation_log"] == "Lead fallback." and final["task_results"] == {}


async def test_a_plan_with_a_cycle_falls_back_to_the_lead_session(demo_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(implementer, "run_implementation", lambda ws, strategy, settings: "Lead fallback.")
    graph = build_graph(MemorySaver())
    config = run_config("engine-cycle")
    await to_implement_gate(graph, config)
    bundle = Path((await graph.aget_state(config)).values["project_bundle_dir"])
    import json

    plan = json.loads((bundle / "plan.json").read_text())
    plan["tasks"][0]["depends_on"] = ["T03"]  # T01 -> T03 -> ... -> T01
    (bundle / "plan.json").write_text(json.dumps(plan))
    await implement(graph, config)
    assert (await graph.aget_state(config)).values["implementation_log"] == "Lead fallback."


# --------------------------------------------------------------- usage and cost


async def test_session_costs_are_added_to_the_usage_records(demo_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def costly(workspace: Path, task: Any, settings: Any, *, budget: Any, emit: Any, **kwargs: Any) -> TaskResult:
        result: TaskResult = {
            "task_id": task.id, "status": "done", "summary": f"Built {task.id}.", "cost_usd": 0.7,
            "turns": 4, "session_id": None, "commit": None,
        }  # fmt: skip
        budget.charge(0.7)
        save_result(workspace, result)
        return result

    monkeypatch.setattr(executor, "run_task", costly)
    graph = build_graph(MemorySaver())
    config = run_config("engine-usage")
    await to_implement_gate(graph, config)
    await implement(graph, config)
    usage = (await graph.aget_state(config)).values["usage"]
    agents = [r for r in usage if r["role"] == "implementer"]
    assert len(agents) == 3 and all(r["provider"] == "anthropic" and r["cost_usd"] == 0.7 for r in agents)
    assert summarize_usage(usage)["cost_usd"] == pytest.approx(2.1)


# ----------------------------------------------------------- the workspace step


def test_the_workspace_is_prepared_by_its_own_checkpointed_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    seen: list[tuple[Path, Path]] = []
    monkeypatch.setattr(implementer, "prepare_workspace", lambda bundle, root: seen.append((bundle, root)) or workspace)
    state = dict(make_initial_state(IDEA, 1))
    state["project_bundle_dir"] = str(tmp_path / "bundle")
    update = implement_nodes.prepare_workspace_node(state)  # type: ignore[arg-type]
    assert update == {"workspace_dir": str(workspace), "stage": "implementation"}
    assert seen[0][0] == tmp_path / "bundle"


def test_the_decline_path_never_creates_a_workspace(demo_env: Path) -> None:
    graph = build_graph(MemorySaver())
    config = run_config("engine-decline")

    async def go() -> None:
        await to_implement_gate(graph, config)
        await graph.ainvoke(Command(resume={"implement": False, "notes": ""}), config)

    asyncio.run(go())
    assert not (demo_env / "projects").exists() or not list((demo_env / "projects").iterdir())

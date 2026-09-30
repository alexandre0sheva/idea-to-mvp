"""The parallel implementation subgraph: worktrees, bounded concurrency, merges, conflicts, resume."""

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from plan_helpers import make_plan, make_task

from idea_to_mvp.config import clear_settings_cache
from idea_to_mvp.implementation import executor, merge
from idea_to_mvp.implementation.events import make_event
from idea_to_mvp.implementation.progress import TaskResult, load_progress, save_result
from idea_to_mvp.implementation.workspace import prepare_workspace
from idea_to_mvp.nodes.implement_graph import build_implement_subgraph
from idea_to_mvp.plan import Plan

STRATEGY = {"mode": "agent_team", "reasoning": "r", "workstreams": [{"name": "backend-api"}, {"name": "web-ui"}]}


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "out"))
    for name, value in (
        ("DEMO_MODE", "false"),
        ("IMPLEMENTER_MAX_PARALLEL", "3"),
        ("IMPLEMENTER_MAX_TOTAL_USD", "100"),
        ("IMPLEMENTER_MAX_TASK_USD", "5"),
    ):
        monkeypatch.setenv(name, value)
    clear_settings_cache()
    yield
    clear_settings_cache()


def set_env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    clear_settings_cache()


def diamond() -> Plan:
    return make_plan(
        [
            make_task("T01", title="Setup", workstream="backend-api"),
            make_task("T02", title="Left", workstream="backend-api", depends_on=["T01"]),
            make_task("T03", title="Right", workstream="web-ui", depends_on=["T01"]),
            make_task("T04", title="Join", workstream="web-ui", depends_on=["T02", "T03"]),
        ]
    )


def independent(count: int) -> Plan:
    return make_plan([make_task(f"T0{i}", title=f"Task {i}") for i in range(1, count + 1)])


def workspace_for(plan: Plan, tmp_path: Path) -> Path:
    bundle = tmp_path / "blueprints" / "20260101-000000-000000-idea"
    bundle.mkdir(parents=True)
    (bundle / "README.md").write_text("line one\nline two\nline three\n")
    (bundle / "plan.json").write_text(plan.model_dump_json(indent=2))
    return prepare_workspace(bundle, tmp_path / "projects")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class FakeTasks:
    """Stands in for `executor.run_task`: real files and commits in the real worktree, fake agent."""

    def __init__(self) -> None:
        self.active = 0
        self.peak = 0
        self.delay = 0.05
        self.cost = 0.0
        self.fail: set[str] = set()
        self.files: dict[str, dict[str, str]] = {}  # task id -> {path: content}; default: src/<id>.py
        self.calls: list[dict[str, Any]] = []

    async def run_task(self, workspace: Path, task: Any, settings: Any, *, budget: Any, emit: Any, **kwargs: Any) -> TaskResult:
        self.calls.append({"task": task.id, "workspace": workspace, **kwargs, "budget": budget.remaining()})
        emit(make_event("task_start", task_id=task.id, label=task.title))
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(self.delay)
        self.active -= 1
        work_dir: Path = kwargs["work_dir"]
        ok = task.id not in self.fail
        commit = None
        if ok:
            for name, content in self.files.get(task.id, {f"src/{task.id.lower()}.py": f"# {task.id}\n"}).items():
                (work_dir / name).parent.mkdir(parents=True, exist_ok=True)
                (work_dir / name).write_text(content)
            commit = await asyncio.to_thread(merge.commit_worktree, workspace, work_dir, task.id, f"{task.id}: {task.title}")
        budget.charge(self.cost)
        return {
            "task_id": task.id,
            "status": "done" if ok else "failed",
            "summary": f"Built {task.id}." if ok else f"{task.id} went wrong.",
            "cost_usd": self.cost,
            "turns": 3,
            "session_id": None,
            "commit": commit,
        }


@pytest.fixture()
def tasks(monkeypatch: pytest.MonkeyPatch) -> FakeTasks:
    fake = FakeTasks()
    monkeypatch.setattr(executor, "run_task", fake.run_task)
    return fake


async def run_graph(workspace: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    graph = build_implement_subgraph()
    events: list[dict[str, Any]] = []
    final: dict[str, Any] = {}
    async for mode, payload in graph.astream(
        {"workspace_dir": str(workspace), "execution_strategy": STRATEGY}, stream_mode=["custom", "values"]
    ):
        if mode == "custom":
            events.append(payload)
        else:
            final = payload
    return final, events


def statuses(state: dict[str, Any]) -> dict[str, str]:
    return {task_id: result["status"] for task_id, result in state["task_results"].items()}


# ----------------------------------------------------------------- scheduling


async def test_the_two_branches_of_a_diamond_run_at_the_same_time_after_the_root(tasks: FakeTasks, tmp_path: Path) -> None:
    workspace = workspace_for(diamond(), tmp_path)
    final, events = await run_graph(workspace)
    assert statuses(final) == {"T01": "done", "T02": "done", "T03": "done", "T04": "done"}
    assert tasks.peak == 2
    order = [(e["kind"], e["task_id"]) for e in events if e["kind"] in ("task_start", "task_end") and e["task_id"]]
    assert order.index(("task_start", "T03")) < order.index(("task_end", "T02"))  # T03 started before T02 finished
    assert order.index(("task_end", "T01")) < order.index(("task_start", "T02"))  # but only after the root merged
    assert order.index(("task_end", "T03")) < order.index(("task_start", "T04"))


async def test_no_more_than_max_parallel_tasks_ever_run_at_once(
    tasks: FakeTasks, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_env(monkeypatch, IMPLEMENTER_MAX_PARALLEL="2")
    workspace = workspace_for(independent(5), tmp_path)
    final, _ = await run_graph(workspace)
    assert tasks.peak == 2 and len(tasks.calls) == 5 and set(statuses(final).values()) == {"done"}
    set_env(monkeypatch, IMPLEMENTER_MAX_PARALLEL="3")
    tasks.peak = 0
    await run_graph(workspace_for(independent(5), tmp_path / "again"))
    assert tasks.peak == 3


async def test_each_session_runs_in_its_own_worktree_with_a_private_budget(tasks: FakeTasks, tmp_path: Path) -> None:
    workspace = workspace_for(independent(2), tmp_path)
    await run_graph(workspace)
    for call in tasks.calls:
        assert call["work_dir"] == merge.worktree_path(workspace, call["task"]) and call["work_dir"] != workspace
        assert call["persist"] is False and call["announce_end"] is False  # the merge step records the outcome
        assert call["budget"] == 5.0  # one task's reservation, not the whole run's budget


# --------------------------------------------------------------------- merging


async def test_branches_are_merged_in_task_id_order_and_the_work_lands_on_main(tasks: FakeTasks, tmp_path: Path) -> None:
    workspace = workspace_for(diamond(), tmp_path)
    final, _ = await run_graph(workspace)
    for name in ("t01", "t02", "t03", "t04"):
        assert (workspace / "src" / f"{name}.py").exists()
    subjects = git(workspace, "log", "--first-parent", "--format=%s").splitlines()
    assert subjects.index("T03: Right (merged)") < subjects.index("T02: Left")  # T02 fast-forwarded, T03 merged after it
    assert git(workspace, "status", "--porcelain") == ""
    assert final["task_results"]["T04"]["commit"] == git(workspace, "rev-parse", "--short", "HEAD")
    assert final["task_results"]["T01"]["commit"]


async def test_done_is_recorded_only_after_the_merge_and_the_worktrees_are_cleaned_up(tasks: FakeTasks, tmp_path: Path) -> None:
    workspace = workspace_for(diamond(), tmp_path)
    await run_graph(workspace)
    assert {k: v["status"] for k, v in load_progress(workspace).items()} == {"T01": "done", "T02": "done", "T03": "done", "T04": "done"}
    assert not (workspace.parent / ".worktrees").exists()
    assert "task/" not in git(workspace, "branch", "--list")


async def test_the_result_carries_a_log_the_usage_and_the_stage(tasks: FakeTasks, tmp_path: Path) -> None:
    tasks.cost = 0.75
    workspace = workspace_for(diamond(), tmp_path)
    final, _ = await run_graph(workspace)
    assert "## T01 — Setup: done" in final["implementation_log"] and "## T04 — Join: done" in final["implementation_log"]
    assert [r["cost_usd"] for r in final["usage"]] == [0.75] * 4 and final["usage"][0]["role"] == "implementer"
    assert final["stage"] == "verification"
    assert git(workspace, "log", "-1", "--format=%s")  # committed state, nothing pending
    assert git(workspace, "status", "--porcelain") == ""


# --------------------------------------------------------------------- failures


async def test_a_failed_session_leaves_main_untouched_and_blocks_only_its_dependents(tasks: FakeTasks, tmp_path: Path) -> None:
    tasks.fail = {"T02"}
    workspace = workspace_for(diamond(), tmp_path)
    final, _ = await run_graph(workspace)
    assert statuses(final) == {"T01": "done", "T02": "failed", "T03": "done", "T04": "skipped"}
    assert [c["task"] for c in tasks.calls] == ["T01", "T02", "T03"]
    assert not (workspace / "src" / "t02.py").exists() and (workspace / "src" / "t03.py").exists()
    assert "T02" in final["task_results"]["T04"]["summary"]
    assert load_progress(workspace)["T04"]["status"] == "skipped" and not (workspace.parent / ".worktrees").exists()


# ----------------------------------------------------------------------- budget


async def test_only_as_many_tasks_start_as_the_remaining_budget_can_cover_at_the_task_cap(
    tasks: FakeTasks, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_env(monkeypatch, IMPLEMENTER_MAX_TOTAL_USD="12", IMPLEMENTER_MAX_TASK_USD="5", IMPLEMENTER_MAX_PARALLEL="4")
    tasks.cost = 5.0
    workspace = workspace_for(independent(4), tmp_path)
    final, _ = await run_graph(workspace)
    # floor(12 / 5) = 2 reservations fit; then 2.0 is left, less than a task's 5.0: nothing more starts.
    assert statuses(final) == {"T01": "done", "T02": "done", "T03": "failed", "T04": "failed"}
    assert "budget" in final["task_results"]["T03"]["summary"].lower() and len(tasks.calls) == 2
    assert sum(r["cost_usd"] for r in final["task_results"].values()) <= 12


async def test_the_task_cap_never_exceeds_the_whole_run_budget(tasks: FakeTasks, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    set_env(monkeypatch, IMPLEMENTER_MAX_TOTAL_USD="3", IMPLEMENTER_MAX_TASK_USD="5")
    tasks.cost = 3.0
    workspace = workspace_for(independent(3), tmp_path)
    final, _ = await run_graph(workspace)
    assert [c["budget"] for c in tasks.calls] == [3.0]  # one task, reserving the 3.0 that exists
    assert sorted(statuses(final).values()) == ["done", "failed", "failed"]


async def test_spending_from_an_earlier_run_counts(tasks: FakeTasks, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    set_env(monkeypatch, IMPLEMENTER_MAX_TOTAL_USD="12", IMPLEMENTER_MAX_TASK_USD="5")
    workspace = workspace_for(independent(3), tmp_path)
    save_result(workspace, {"task_id": "T01", "status": "done", "summary": "earlier", "cost_usd": 8.0, "turns": 1, "session_id": None, "commit": None})
    final, _ = await run_graph(workspace)
    assert [c["task"] for c in tasks.calls] == []  # 12 - 8 = 4 left: less than one task's 5.0 reservation
    assert statuses(final) == {"T01": "done", "T02": "failed", "T03": "failed"}


# --------------------------------------------------------------------- conflicts


def conflicting_files(tasks: FakeTasks) -> None:
    tasks.files = {
        "T02": {"README.md": "line one\nLEFT\nline three\n"},
        "T03": {"README.md": "line one\nRIGHT\nline three\n"},
    }


def two_branch_plan() -> Plan:
    return make_plan(
        [
            make_task("T01", title="Setup"),
            make_task("T02", title="Left", workstream="backend-api", depends_on=["T01"]),
            make_task("T03", title="Right", workstream="web-ui", depends_on=["T01"]),
            make_task("T04", title="Join", depends_on=["T03"]),
        ]
    )


async def test_a_merge_conflict_is_handed_to_one_resolver_session(
    tasks: FakeTasks, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conflicting_files(tasks)
    seen: list[dict[str, Any]] = []

    async def resolver(workspace: Path, task: Any, files: list[str], settings: Any, **kwargs: Any) -> Any:
        seen.append({"task": task.id, "files": files})
        (workspace / "README.md").write_text("line one\nLEFT and RIGHT\nline three\n")
        return executor.ResolverOutcome(success=True, summary="kept both", cost_usd=0.4)

    monkeypatch.setattr(executor, "run_merge_resolver", resolver)
    workspace = workspace_for(two_branch_plan(), tmp_path)
    final, events = await run_graph(workspace)
    assert seen == [{"task": "T03", "files": ["README.md"]}]
    assert statuses(final) == {"T01": "done", "T02": "done", "T03": "done", "T04": "done"}
    assert (workspace / "README.md").read_text() == "line one\nLEFT and RIGHT\nline three\n"
    assert final["task_results"]["T03"]["cost_usd"] == pytest.approx(0.4)  # the resolver's spend is the task's
    assert any("conflict" in e["detail"].lower() for e in events if e["kind"] == "text")
    assert git(workspace, "status", "--porcelain") == ""


async def test_an_unresolvable_conflict_fails_that_task_aborts_the_merge_and_skips_its_dependents(
    tasks: FakeTasks, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conflicting_files(tasks)

    async def giving_up(workspace: Path, task: Any, files: list[str], settings: Any, **kwargs: Any) -> Any:
        return executor.ResolverOutcome(success=False, summary="too tangled", cost_usd=0.2)

    monkeypatch.setattr(executor, "run_merge_resolver", giving_up)
    workspace = workspace_for(two_branch_plan(), tmp_path)
    final, _ = await run_graph(workspace)
    assert statuses(final) == {"T01": "done", "T02": "done", "T03": "failed", "T04": "skipped"}
    assert "conflict" in final["task_results"]["T03"]["summary"].lower()
    assert (workspace / "README.md").read_text() == "line one\nLEFT\nline three\n"  # T02's version stands
    assert git(workspace, "status", "--porcelain") == "" and merge.conflicted_files(workspace) == []
    assert not (workspace.parent / ".worktrees").exists()


async def test_a_resolver_that_leaves_markers_behind_does_not_count_as_success(
    tasks: FakeTasks, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conflicting_files(tasks)

    async def sloppy(workspace: Path, task: Any, files: list[str], settings: Any, **kwargs: Any) -> Any:
        return executor.ResolverOutcome(success=True, summary="done (it says)", cost_usd=0.1)  # touched nothing

    monkeypatch.setattr(executor, "run_merge_resolver", sloppy)
    workspace = workspace_for(two_branch_plan(), tmp_path)
    final, _ = await run_graph(workspace)
    assert statuses(final)["T03"] == "failed" and git(workspace, "status", "--porcelain") == ""


async def test_no_resolver_is_started_when_no_budget_is_left_for_it(
    tasks: FakeTasks, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_env(monkeypatch, IMPLEMENTER_MAX_TOTAL_USD="10", IMPLEMENTER_MAX_TASK_USD="5", IMPLEMENTER_MAX_PARALLEL="2")
    conflicting_files(tasks)
    tasks.cost = 5.0  # both sessions together use the whole budget
    called: list[str] = []

    async def resolver(*args: Any, **kwargs: Any) -> Any:
        called.append("resolver")
        return executor.ResolverOutcome(success=True, summary="", cost_usd=0.0)

    monkeypatch.setattr(executor, "run_merge_resolver", resolver)
    plan = make_plan([make_task("T02", title="Left"), make_task("T03", title="Right", workstream="web-ui")])
    workspace = workspace_for(plan, tmp_path)
    final, _ = await run_graph(workspace)
    assert called == []  # 10.0 spent of 10.0: nothing left to pay a resolver with
    assert statuses(final) == {"T02": "done", "T03": "failed"} and "budget" in final["task_results"]["T03"]["summary"].lower()
    assert git(workspace, "status", "--porcelain") == "" and merge.conflicted_files(workspace) == []


# ------------------------------------------------------------------------ resume


async def test_a_resumed_run_skips_merged_tasks_and_discards_half_finished_worktrees(
    tasks: FakeTasks, tmp_path: Path
) -> None:
    workspace = workspace_for(diamond(), tmp_path)
    await run_graph(workspace)
    # Simulate a kill after T01 and T02 merged but before T03 finished: T03 and T04 are not recorded.
    progress = load_progress(workspace)
    path = workspace / ".idea-to-mvp" / "progress.json"
    path.write_text(json.dumps({k: v for k, v in progress.items() if k in ("T01", "T02")}))
    (workspace / "src" / "t03.py").unlink()
    git(workspace, "add", "-A")
    git(workspace, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "undo T03 for the test")
    stale = await merge.create_worktree(workspace, "T03")
    (stale / "half-done.txt").write_text("unfinished")

    tasks.calls.clear()
    final, _ = await run_graph(workspace)
    assert [c["task"] for c in tasks.calls] == ["T03", "T04"]  # T01 and T02 were not run again
    assert statuses(final) == {"T01": "done", "T02": "done", "T03": "done", "T04": "done"}
    assert not (workspace / "half-done.txt").exists() and not (workspace.parent / ".worktrees").exists()

"""The task-by-task engine: one fresh agent session per plan task, committed and resumable."""

import asyncio
import re
import subprocess
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock
from plan_helpers import make_plan, make_task

from idea_to_mvp.config import Settings
from idea_to_mvp.implementation import merge
from idea_to_mvp.implementation import options as opts
from idea_to_mvp.implementation.events import ImplEvent
from idea_to_mvp.implementation.executor import (
    BudgetTracker,
    render_task_log,
    run_merge_resolver,
    run_plan,
    run_task,
)
from idea_to_mvp.implementation.progress import load_progress, save_result
from idea_to_mvp.implementation.workspace import prepare_workspace


@pytest.fixture(autouse=True)
def no_git_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("DEMO_MODE", raising=False)


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    bundle = tmp_path / "blueprints" / "20260101-000000-000000-idea"
    bundle.mkdir(parents=True)
    (bundle / "README.md").write_text("# idea")
    return prepare_workspace(bundle, tmp_path / "projects")


def make_settings(**overrides: Any) -> Settings:
    fields: dict[str, Any] = {
        "implementer_sandbox": "off",
        "implementer_max_total_usd": 25.0,
        "implementer_max_task_usd": 5.0,
        "implementer_max_task_turns": 17,
    }
    fields.update(overrides)
    return Settings(_env_file=None, **fields)


class FakeQuery:
    """Stands in for the SDK's `query`: does some 'work' in the workspace and reports a result."""

    def __init__(self, workspace: Path, *, cost: float = 0.5, turns: int = 5) -> None:
        self.workspace = workspace
        self.cost = cost
        self.turns = turns
        self.calls: list[tuple[str, Any]] = []
        self.fail: set[str] = set()  # task ids whose session ends in an error result
        self.crash: set[str] = set()  # task ids whose session raises
        self.no_changes: set[str] = set()

    def task_of(self, prompt: str) -> str:
        match = re.search(r"task (T\d+)", prompt)
        assert match, prompt
        return match.group(1)

    async def __call__(self, *, prompt: str, options: Any) -> AsyncIterator[Any]:
        self.calls.append((prompt, options))
        task_id = self.task_of(prompt)
        if task_id in self.crash:
            raise RuntimeError(f"agent process died during {task_id}")
        if task_id not in self.no_changes:
            (self.workspace / "src").mkdir(exist_ok=True)
            (self.workspace / "src" / f"{task_id.lower()}.py").write_text(f"# work for {task_id}\n")
        yield AssistantMessage(
            content=[
                TextBlock(text=f"Working on {task_id}."),
                ToolUseBlock(id="1", name="Write", input={"file_path": f"src/{task_id.lower()}.py"}),
            ],
            model="claude-x",
        )
        failed = task_id in self.fail
        yield ResultMessage(
            subtype="error_max_turns" if failed else "success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=failed,
            num_turns=self.turns,
            session_id=f"session-{task_id}",
            total_cost_usd=self.cost,
            result=f"Stopped early on {task_id}." if failed else f"Built {task_id}.",
        )

    def options_for(self, task_id: str) -> Any:
        return next(opts_ for prompt, opts_ in self.calls if self.task_of(prompt) == task_id)

    @property
    def tasks_run(self) -> list[str]:
        return [self.task_of(prompt) for prompt, _ in self.calls]


def git_log(workspace: Path) -> list[str]:
    out = subprocess.run(
        ["git", "log", "--format=%s"], cwd=workspace, capture_output=True, text=True, check=True
    ).stdout
    return out.splitlines()


def head(workspace: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=workspace, capture_output=True, text=True, check=True
    ).stdout.strip()


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------- BudgetTracker


def test_the_budget_tracker_counts_down_and_never_goes_negative() -> None:
    budget = BudgetTracker(10.0)
    assert budget.remaining() == 10.0
    budget.charge(3.5)
    assert budget.remaining() == 6.5 and budget.spent == 3.5
    budget.charge(100.0)
    assert budget.remaining() == 0.0


def test_the_budget_tracker_ignores_negative_charges_and_survives_threads() -> None:
    budget = BudgetTracker(1000.0)
    budget.charge(-5.0)
    assert budget.remaining() == 1000.0
    threads = [threading.Thread(target=lambda: [budget.charge(0.01) for _ in range(500)]) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert budget.spent == pytest.approx(40.0)


# --------------------------------------------------------------------- run_task


def test_a_successful_task_is_committed_recorded_and_charged(workspace: Path) -> None:
    query = FakeQuery(workspace, cost=0.42, turns=9)
    events: list[ImplEvent] = []
    budget = BudgetTracker(25.0)
    task = make_task("T01", title="Project setup")

    result = run(run_task(workspace, task, make_settings(), budget=budget, emit=events.append, query_fn=query))

    assert git_log(workspace)[0] == "T01: Project setup"
    assert result["status"] == "done" and result["summary"] == "Built T01."
    assert result["cost_usd"] == 0.42 and result["turns"] == 9 and result["session_id"] == "session-T01"
    assert result["commit"] == head(workspace)
    assert load_progress(workspace)["T01"] == result
    assert budget.remaining() == pytest.approx(24.58)
    assert events[0]["kind"] == "task_start" and events[-1]["kind"] == "task_end"
    assert {"tool", "text", "cost"} <= {e["kind"] for e in events}
    assert events[-1]["detail"].startswith("done") and all(e["task_id"] == "T01" for e in events)


def test_the_orchestrator_commits_and_the_progress_file_stays_out_of_git(workspace: Path) -> None:
    query = FakeQuery(workspace)
    run(run_task(workspace, make_task("T01"), make_settings(), budget=BudgetTracker(25), emit=lambda e: None, query_fn=query))
    tracked = subprocess.run(["git", "ls-files"], cwd=workspace, capture_output=True, text=True).stdout.split()
    assert "src/t01.py" in tracked and not any(name.startswith(".idea-to-mvp") for name in tracked)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=workspace, capture_output=True, text=True).stdout
    assert status == ""


def test_the_task_prompt_carries_everything_the_agent_needs(workspace: Path) -> None:
    plan = make_plan(
        [
            make_task("T01", title="Setup"),
            make_task(
                "T02",
                title="Logging",
                goal="Let a climber log a climb.",
                scope="Form plus API call.",
                depends_on=["T01"],
                requirement_ids=["R1", "R2"],
                contracts_in=["C1"],
                contracts_out=["C2"],
                acceptance=["A climb can be logged", "Invalid input is rejected"],
                tests=["Form submission test"],
                coverage_target=90,
            ),
        ]
    )
    save_result(workspace, {"task_id": "T01", "status": "done", "summary": "Scaffolded the FastAPI app.", "cost_usd": 1.0, "turns": 3, "session_id": None, "commit": None})
    query = FakeQuery(workspace)
    run(run_task(workspace, plan.tasks[1], make_settings(), budget=BudgetTracker(25), emit=lambda e: None, plan=plan, query_fn=query))
    prompt = query.calls[0][0]
    for needle in (
        "task T02",
        "Logging",
        "Let a climber log a climb.",
        "Form plus API call.",
        "R1, R2",
        "- A climb can be logged",
        "- Invalid input is rejected",
        "- Form submission test",
        "90%",
        "C1",
        "C2",
        "T01",
        "Scaffolded the FastAPI app.",  # what the dependency reported
        "AGENTS.md",
        "pytest",  # the plan's test command
    ):
        assert needle in prompt, needle
    assert "Do not run `git commit`" in prompt and "only this task" in prompt.lower()


def test_the_sdk_budget_is_the_smaller_of_the_task_cap_and_what_is_left(workspace: Path) -> None:
    settings = make_settings(implementer_max_task_usd=5.0)
    query = FakeQuery(workspace, cost=0.0)
    run(run_task(workspace, make_task("T01"), settings, budget=BudgetTracker(25.0), emit=lambda e: None, query_fn=query))
    assert query.options_for("T01").max_budget_usd == 5.0
    assert query.options_for("T01").max_turns == 17  # implementer_max_task_turns
    run(run_task(workspace, make_task("T02"), settings, budget=BudgetTracker(1.75), emit=lambda e: None, query_fn=query))
    assert query.options_for("T02").max_budget_usd == pytest.approx(1.75)


def test_the_session_is_confined_like_every_other_agent_session(workspace: Path) -> None:
    query = FakeQuery(workspace)
    run(run_task(workspace, make_task("T01"), make_settings(), budget=BudgetTracker(25), emit=lambda e: None, query_fn=query))
    options = query.options_for("T01")
    assert Path(str(options.cwd)) == workspace and options.setting_sources == [] and options.hooks


def test_an_exhausted_budget_fails_the_task_without_starting_a_session(workspace: Path) -> None:
    query = FakeQuery(workspace)
    events: list[ImplEvent] = []
    result = run(run_task(workspace, make_task("T01"), make_settings(), budget=BudgetTracker(0.0), emit=events.append, query_fn=query))
    assert query.calls == [] and result["status"] == "failed" and "budget" in result["summary"].lower()
    assert load_progress(workspace)["T01"]["status"] == "failed"
    assert events[-1]["kind"] == "task_end" and "budget" in events[-1]["detail"].lower()
    assert git_log(workspace) == ["blueprint"]


def test_a_task_that_is_already_done_is_skipped_on_a_rerun(workspace: Path) -> None:
    query = FakeQuery(workspace, cost=0.3)
    budget = BudgetTracker(25.0)
    first = run(run_task(workspace, make_task("T01"), make_settings(), budget=budget, emit=lambda e: None, query_fn=query))
    events: list[ImplEvent] = []
    again = run(run_task(workspace, make_task("T01"), make_settings(), budget=budget, emit=events.append, query_fn=query))
    assert query.tasks_run == ["T01"] and again == first  # no second session, the stored result comes back
    assert budget.remaining() == pytest.approx(24.7)  # and nothing is charged twice
    assert [e["kind"] for e in events] == ["task_end"] and "earlier run" in events[0]["detail"]


def test_a_failed_task_is_retried_on_the_next_run(workspace: Path) -> None:
    query = FakeQuery(workspace)
    query.fail = {"T01"}
    failed = run(run_task(workspace, make_task("T01"), make_settings(), budget=BudgetTracker(25), emit=lambda e: None, query_fn=query))
    assert failed["status"] == "failed" and failed["commit"] is None and "Stopped early" in failed["summary"]
    assert git_log(workspace) == ["blueprint"]  # a failed session is never committed
    query.fail = set()
    retried = run(run_task(workspace, make_task("T01"), make_settings(), budget=BudgetTracker(25), emit=lambda e: None, query_fn=query))
    assert retried["status"] == "done" and query.tasks_run == ["T01", "T01"]
    assert load_progress(workspace)["T01"]["status"] == "done"


def test_a_failed_session_is_still_charged(workspace: Path) -> None:
    query = FakeQuery(workspace, cost=1.25)
    query.fail = {"T01"}
    budget = BudgetTracker(25.0)
    result = run(run_task(workspace, make_task("T01"), make_settings(), budget=budget, emit=lambda e: None, query_fn=query))
    assert result["cost_usd"] == 1.25 and budget.remaining() == pytest.approx(23.75)


def test_a_session_that_crashes_becomes_a_failed_task_not_a_crash(workspace: Path) -> None:
    query = FakeQuery(workspace)
    query.crash = {"T01"}
    result = run(run_task(workspace, make_task("T01"), make_settings(), budget=BudgetTracker(25), emit=lambda e: None, query_fn=query))
    assert result["status"] == "failed" and "agent process died" in result["summary"]
    assert load_progress(workspace)["T01"]["status"] == "failed"


def test_a_missing_sandbox_is_a_configuration_error_not_a_failed_task(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(opts, "sandbox_supported", lambda *a, **k: False)
    with pytest.raises(opts.SandboxUnavailableError):
        run(run_task(workspace, make_task("T01"), make_settings(implementer_sandbox="on"), budget=BudgetTracker(25), emit=lambda e: None, query_fn=FakeQuery(workspace)))
    assert load_progress(workspace) == {}


def test_a_task_that_changes_nothing_is_done_without_a_commit(workspace: Path) -> None:
    query = FakeQuery(workspace)
    query.no_changes = {"T01"}
    result = run(run_task(workspace, make_task("T01"), make_settings(), budget=BudgetTracker(25), emit=lambda e: None, query_fn=query))
    assert result["status"] == "done" and result["commit"] is None
    assert git_log(workspace) == ["blueprint"]


# --------------------------------------------------------------------- run_plan


def diamond():
    return make_plan(
        [
            make_task("T01", title="Setup", requirement_ids=["R1", "R2"]),
            make_task("T02", title="Left", depends_on=["T01"]),
            make_task("T03", title="Right", depends_on=["T01"]),
            make_task("T04", title="Join", depends_on=["T02", "T03"]),
        ]
    )


def test_a_plan_runs_its_waves_in_order_and_commits_each_task(workspace: Path) -> None:
    query = FakeQuery(workspace)
    results = run(run_plan(workspace, diamond(), make_settings(), emit=lambda e: None, query_fn=query))
    assert query.tasks_run == ["T01", "T02", "T03", "T04"]
    assert [r["status"] for r in results] == ["done"] * 4 and [r["task_id"] for r in results] == ["T01", "T02", "T03", "T04"]
    assert git_log(workspace)[:4] == ["T04: Join", "T03: Right", "T02: Left", "T01: Setup"]


def test_the_position_of_every_task_is_announced(workspace: Path) -> None:
    events: list[ImplEvent] = []
    run(run_plan(workspace, diamond(), make_settings(), emit=events.append, query_fn=FakeQuery(workspace)))
    starts = [(e["task_id"], e["detail"]) for e in events if e["kind"] == "task_start"]
    assert starts == [("T01", "1/4"), ("T02", "2/4"), ("T03", "3/4"), ("T04", "4/4")]


def test_tasks_that_depend_on_a_failed_task_are_skipped_but_independent_ones_still_run(workspace: Path) -> None:
    query = FakeQuery(workspace)
    query.fail = {"T02"}
    results = {r["task_id"]: r for r in run(run_plan(workspace, diamond(), make_settings(), emit=lambda e: None, query_fn=query))}
    assert {k: v["status"] for k, v in results.items()} == {"T01": "done", "T02": "failed", "T03": "done", "T04": "skipped"}
    assert query.tasks_run == ["T01", "T02", "T03"]
    assert "T02" in results["T04"]["summary"] and load_progress(workspace)["T04"]["status"] == "skipped"


def test_the_whole_run_budget_is_shared_by_all_tasks(workspace: Path) -> None:
    query = FakeQuery(workspace, cost=3.0)
    settings = make_settings(implementer_max_total_usd=7.0, implementer_max_task_usd=5.0)
    results = run(run_plan(workspace, diamond(), settings, emit=lambda e: None, query_fn=query))
    budgets = [query.options_for(t).max_budget_usd for t in ("T01", "T02", "T03")]
    assert budgets == [5.0, 4.0, 1.0]  # min(task cap, what is left) each time
    assert [r["status"] for r in results] == ["done", "done", "done", "failed"]
    assert "budget" in results[3]["summary"].lower() and len(query.calls) == 3


def test_a_resumed_run_counts_what_earlier_runs_already_spent(workspace: Path) -> None:
    save_result(workspace, {"task_id": "T01", "status": "done", "summary": "earlier", "cost_usd": 4.0, "turns": 5, "session_id": "s", "commit": None})
    events: list[ImplEvent] = []
    query = FakeQuery(workspace, cost=0.0)
    settings = make_settings(implementer_max_total_usd=7.0, implementer_max_task_usd=5.0)
    run(run_plan(workspace, diamond(), settings, emit=events.append, query_fn=query))
    assert query.tasks_run == ["T02", "T03", "T04"]  # T01 is not run again
    assert query.options_for("T02").max_budget_usd == pytest.approx(3.0)  # 7.0 - 4.0 already spent
    earlier = [e for e in events if e["kind"] == "cost" and e["label"] == "earlier runs"]
    assert len(earlier) == 1 and earlier[0]["cost_usd"] == 4.0


def test_the_task_log_summarises_every_result(workspace: Path) -> None:
    query = FakeQuery(workspace, cost=0.5, turns=4)
    query.fail = {"T03"}
    plan = diamond()
    results = run(run_plan(workspace, plan, make_settings(), emit=lambda e: None, query_fn=query))
    log = render_task_log(plan, results)
    assert "## T01 — Setup: done" in log and "## T03 — Right: failed" in log and "## T04 — Join: skipped" in log
    assert "$0.50" in log and "4 turns" in log and "Built T01." in log and "Stopped early on T03." in log


# -------------------------------------------------------------------- demo mode


def test_demo_mode_builds_a_real_project_task_by_task_without_the_sdk(workspace: Path) -> None:
    from idea_to_mvp.demo.implementer import demo_verify_lane

    events: list[ImplEvent] = []
    plan = make_plan([make_task("T01", title="Setup"), make_task("T02", title="Core", depends_on=["T01"])])
    results = run(run_plan(workspace, plan, make_settings(demo_mode=True), emit=events.append))
    assert [r["status"] for r in results] == ["done", "done"]
    assert git_log(workspace)[:2] == ["T02: Core", "T01: Setup"]
    assert (workspace / "demo_app").is_dir() and (workspace / "notes" / "T02.md").exists()
    assert demo_verify_lane(workspace, "tests").passed is True
    assert {"task_start", "tool", "text", "task_end"} <= {e["kind"] for e in events}
    assert load_progress(workspace)["T02"]["status"] == "done" and results[0]["cost_usd"] == 0.0


# ------------------------------------------------------------ worktree mode (parallel tasks)


def test_a_task_in_a_worktree_runs_and_commits_there_and_leaves_recording_to_the_merge_step(workspace: Path) -> None:
    worktree = run(merge.create_worktree(workspace, "T02"))
    query = FakeQuery(worktree)
    events: list[ImplEvent] = []
    plan = make_plan([make_task("T01"), make_task("T02", title="Left", depends_on=["T01"])])
    result = run(
        run_task(
            workspace, plan.tasks[1], make_settings(), budget=BudgetTracker(5.0), emit=events.append, plan=plan,
            work_dir=worktree, persist=False, announce_end=False, query_fn=query,
        )
    )  # fmt: skip
    assert Path(str(query.options_for("T02").cwd)) == worktree  # the session's sandbox root is the worktree
    assert result["status"] == "done" and result["commit"] == head_of(worktree)
    assert git_log(worktree)[0] == "T02: Left" and git_log(workspace) == ["blueprint"]  # main is untouched until merged
    assert load_progress(workspace) == {}  # `done` is recorded only after the merge
    assert [e["kind"] for e in events if e["kind"] in ("task_start", "task_end")] == ["task_start"]


def head_of(path: Path) -> str:
    return head(path)


def test_the_prompt_of_a_worktree_task_says_dependencies_are_not_installed(workspace: Path) -> None:
    worktree = run(merge.create_worktree(workspace, "T01"))
    query = FakeQuery(worktree)
    plan = make_plan([make_task("T01")])
    run(run_task(workspace, plan.tasks[0], make_settings(), budget=BudgetTracker(5.0), emit=lambda e: None, plan=plan, work_dir=worktree, persist=False, query_fn=query))
    prompt = query.calls[0][0]
    assert "worktree" in prompt.lower() and "install command" in prompt.lower() and "merged back" in prompt.lower()
    sequential = FakeQuery(workspace)
    run(run_task(workspace, make_task("T09"), make_settings(), budget=BudgetTracker(5.0), emit=lambda e: None, plan=plan, query_fn=sequential))
    assert "worktree" not in sequential.calls[0][0].lower()


def test_a_done_task_is_still_skipped_when_it_would_run_in_a_worktree(workspace: Path) -> None:
    save_result(workspace, {"task_id": "T01", "status": "done", "summary": "earlier", "cost_usd": 1.0, "turns": 2, "session_id": None, "commit": "abc"})
    query = FakeQuery(workspace)
    result = run(run_task(workspace, make_task("T01"), make_settings(), budget=BudgetTracker(5.0), emit=lambda e: None, work_dir=workspace / "nowhere", persist=False, query_fn=query))
    assert query.calls == [] and result["summary"] == "earlier"


# ------------------------------------------------------------------ the merge resolver


def conflict_workspace(workspace: Path) -> list[str]:
    left = run(merge.create_worktree(workspace, "T02"))
    right = run(merge.create_worktree(workspace, "T03"))
    (left / "README.md").write_text("LEFT\n")
    (right / "README.md").write_text("RIGHT\n")
    (workspace / "README.md").write_text("base\n")
    subprocess.run(["git", "add", "-A"], cwd=workspace, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"], cwd=workspace, check=True)
    return ["README.md"]


def test_the_resolver_session_gets_the_conflicts_the_task_and_the_test_command(workspace: Path) -> None:
    plan = make_plan([make_task("T03", title="Right", goal="Build the right half.")])
    query = FakeQuery(workspace, cost=0.3, turns=6)
    query.task_of = lambda prompt: "T03"  # type: ignore[method-assign]
    outcome = run(
        run_merge_resolver(workspace, plan.tasks[0], ["README.md", "src/app.py"], make_settings(), emit=lambda e: None, budget_usd=2.5, plan=plan, query_fn=query)
    )
    assert outcome.success and outcome.cost_usd == 0.3 and outcome.summary == "Built T03."
    prompt, options = query.calls[0]
    for needle in ("README.md", "src/app.py", "T03", "Build the right half.", "pytest", "conflict markers", "Do not run `git commit`"):
        assert needle in prompt, needle
    assert Path(str(options.cwd)) == workspace  # resolves in the main workspace, where the merge is in progress
    assert options.max_budget_usd == 2.5 and options.max_turns == 17


def test_a_resolver_that_fails_or_crashes_reports_failure_without_raising(workspace: Path) -> None:
    plan = make_plan([make_task("T03")])
    failing = FakeQuery(workspace)
    failing.fail = {"T03"}
    outcome = run(run_merge_resolver(workspace, plan.tasks[0], ["a"], make_settings(), emit=lambda e: None, budget_usd=1.0, plan=plan, query_fn=failing))
    assert not outcome.success and "Stopped early" in outcome.summary
    crashing = FakeQuery(workspace)
    crashing.crash = {"T03"}
    outcome = run(run_merge_resolver(workspace, plan.tasks[0], ["a"], make_settings(), emit=lambda e: None, budget_usd=1.0, plan=plan, query_fn=crashing))
    assert not outcome.success and "agent process died" in outcome.summary


def test_the_resolver_is_a_no_op_success_in_demo_mode(workspace: Path) -> None:
    plan = make_plan([make_task("T03")])
    outcome = run(run_merge_resolver(workspace, plan.tasks[0], ["a"], make_settings(demo_mode=True), emit=lambda e: None, budget_usd=1.0, plan=plan))
    assert outcome.success and outcome.cost_usd == 0.0

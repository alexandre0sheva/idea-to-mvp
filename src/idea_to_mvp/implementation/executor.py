"""The task-by-task implementation engine: one fresh agent session per plan task.

A fresh context per task is more reliable than one long session, cost and progress are visible per
task, a killed run resumes at the first unfinished task (`progress.py`), and a task is the unit that
parallel execution needs. After a task succeeds the *orchestrator* commits (`T01: <title>`); a failed
session is never committed. All sessions share one `BudgetTracker`, so the whole run has a cost cap.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from idea_to_mvp import implementer
from idea_to_mvp.implementation import merge
from idea_to_mvp.implementation.events import ImplEvent, events_from_sdk_message, make_event
from idea_to_mvp.implementation.integrity import IntegrityFinding, SessionWatch
from idea_to_mvp.implementation.ledger import SessionOutcome, tuning_spend
from idea_to_mvp.implementation.options import SandboxUnavailableError
from idea_to_mvp.implementation.progress import (
    TaskResult,
    load_progress,
    save_result,
    spent_in_iteration,
)
from idea_to_mvp.implementation.trajectory import SessionLog, model_label
from idea_to_mvp.implementation.workspace import commit_workspace, diff_stat, head_commit
from idea_to_mvp.plan import Plan, PlanTask, execution_waves, iteration_of
from idea_to_mvp.prompts_common import ANSWER_FREE, EVIDENCE_BOUNDARY, TEST_INTEGRITY

if TYPE_CHECKING:  # pragma: no cover - typing only
    from idea_to_mvp.config import Settings
    from idea_to_mvp.model_catalog import AgentRole

LOGGER = logging.getLogger(__name__)

_SUMMARY_LIMIT = 2000
_DEPENDENCY_SUMMARY_LIMIT = 400

TASK_PROMPT = """\
You are implementing task {task_id} of this project: {title}.
This is a fresh session. Earlier tasks are already in the workspace; build on them and never rewrite them.

Read `AGENTS.md`, `PRD.md`, `ARCHITECTURE.md`, and `plan.md` first (`plan.md` lists every task; you own only the one below).
Project skills in `.claude/skills/` describe this stack's recurring procedures; use one when its trigger applies.

## Task {task_id}: {title}
- Goal: {goal}
- Workstream: {workstream}
- Requirements: {requirements}
- Implementation scope: {scope}
- Contracts you consume: {contracts_in}
- Contracts you provide: {contracts_out}
- Coverage target: at least {coverage}% for changed scope

### Acceptance criteria
{acceptance}

### Required tests
{tests}

### Already implemented (your dependencies)
{dependencies}
{commands}
{worktree}## Rules
- Implement only this task. Do not start other tasks or change their contracts.
- Every acceptance criterion needs a test, and the project's full test suite must stay green.
- If the project's dependencies are not installed, run the install command first.
- If the project has `scripts/check.sh`, run `bash scripts/check.sh` before you finish: it installs, lints, and
  runs the tests the way verification does.
- Do not run `git commit` or change git configuration; the orchestrator commits when you finish.
- Finish with a short summary of what you built and anything the next tasks must know.

""" + ANSWER_FREE + "\n" + TEST_INTEGRITY


WORKTREE_NOTE = """\
## Where you are working
This checkout is an isolated git worktree made for this task. Dependencies installed in other checkouts
(`node_modules`, virtual environments) are not here, so run the install command before anything else.
Your changes are merged back into the project automatically when you finish.

"""

RESOLVER_PROMPT = """\
You are resolving a git merge conflict in this project.

Task {task_id} ("{title}") was built in a separate worktree and is being merged into the project's main
line, where other tasks have already landed. The merge stopped with conflicts in:
{files}

What task {task_id} was for: {goal}
Its scope: {scope}

Open each conflicted file and resolve every conflict marker (`<<<<<<<`, `=======`, `>>>>>>>`). Keep the
intent of both sides and merge the code rather than picking one side, unless the two are truly
incompatible. Then run the project's test command{test_hint} and fix anything the merge broke.

## Rules
- Change only what is needed to resolve the conflicts and keep the tests green.
- No conflict markers may remain in any file.
- Do not run `git commit`, `git merge`, or `git merge --abort`; the orchestrator completes the merge when you finish.
- Finish with one short paragraph on how you resolved it.

""" + TEST_INTEGRITY + "\n" + EVIDENCE_BOUNDARY


class BudgetTracker:
    """Whole-run spending shared by every session (thread-safe: parallel tasks will charge it)."""

    def __init__(self, total_usd: float) -> None:
        self._total = float(total_usd)
        self._spent = 0.0
        self._lock = threading.Lock()

    @property
    def spent(self) -> float:
        with self._lock:
            return self._spent

    def remaining(self) -> float:
        with self._lock:
            return max(0.0, self._total - self._spent)

    def charge(self, usd: float) -> None:
        if usd > 0:
            with self._lock:
                self._spent += usd


@dataclass
class _Outcome:
    success: bool
    summary: str
    cost_usd: float = 0.0
    turns: int = 0
    session_id: str | None = None
    crashed: bool = False  # the session raised instead of ending with a result


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) or "- (none)"


def build_task_prompt(
    task: PlanTask, plan: Plan | None, progress: dict[str, TaskResult], *, in_worktree: bool = False
) -> str:
    titles = {t.id: t.title for t in plan.tasks} if plan else {}
    dependencies = []
    for dependency in task.depends_on:
        done = progress.get(dependency)
        summary = (done["summary"] if done else "").strip()[:_DEPENDENCY_SUMMARY_LIMIT]
        label = f"{dependency} ({titles[dependency]})" if dependency in titles else dependency
        dependencies.append(f"- {label}: {summary}" if summary else f"- {label}")
    commands = ""
    if plan is not None:
        c = plan.commands
        lines = [f"- {name}: `{value}`" for name, value in (("Install", c.install), ("Test", c.test), ("Lint", c.lint), ("Run", c.run)) if value]
        commands = "\n### Project commands\n" + "\n".join(lines) + "\n"
    return TASK_PROMPT.format(
        task_id=task.id,
        title=task.title,
        goal=task.goal,
        workstream=task.workstream,
        requirements=", ".join(task.requirement_ids) or "none",
        scope=task.scope,
        contracts_in=", ".join(task.contracts_in) or "none",
        contracts_out=", ".join(task.contracts_out) or "none",
        coverage=task.coverage_target,
        acceptance=_bullets(task.acceptance),
        tests=_bullets(task.tests),
        dependencies="\n".join(dependencies) or "- (none: this task has no dependencies)",
        commands=commands,
        worktree=WORKTREE_NOTE if in_worktree else "",
    )


async def _run_session(
    cwd: Path,
    task: PlanTask,
    settings: Settings,
    *,
    prompt: str,
    budget_usd: float,
    emit: Callable[[ImplEvent], None],
    query_fn: Callable[..., AsyncIterator[Any]] | None,
    role: AgentRole = "implementer",
    log: SessionLog | None = None,
) -> _Outcome:
    from claude_agent_sdk import ResultMessage

    outcome = _Outcome(success=False, summary="The session ended without a result.")
    try:
        async for message in implementer.iter_agent_messages(
            prompt,
            workspace=cwd,
            settings=settings,
            max_turns=settings.implementer_max_task_turns,
            max_budget_usd=budget_usd,
            role=role,
            query_fn=query_fn,
        ):
            if log is not None:
                log.message(message)
            for event in events_from_sdk_message(message, task.id):
                emit(event)
            if isinstance(message, ResultMessage):
                text = (message.result or "").strip()
                outcome = _Outcome(
                    success=not message.is_error,
                    summary=(text or f"The session ended with {message.subtype}.")[:_SUMMARY_LIMIT],
                    cost_usd=float(message.total_cost_usd or 0.0),
                    turns=message.num_turns,
                    session_id=message.session_id,
                )
    except SandboxUnavailableError:
        raise  # a configuration problem, not a failed task
    except Exception as exc:  # the agent process died, the API failed, ...: this task fails, the run goes on
        LOGGER.exception("Session for task %s failed", task.id)
        outcome = _Outcome(
            success=False,
            summary=f"The session failed: {type(exc).__name__}: {exc}"[:_SUMMARY_LIMIT],
            cost_usd=outcome.cost_usd,
            turns=outcome.turns,
            session_id=outcome.session_id,
            crashed=True,
        )
    return outcome


def _finish_log(log: SessionLog, outcome: _Outcome) -> None:
    """Close a session's trajectory and add it to the ledger; what went wrong is written into the trajectory."""
    result: SessionOutcome = "crashed" if outcome.crashed else "success" if outcome.success else "failed"
    log.finish(
        outcome=result,
        cost_usd=outcome.cost_usd,
        turns=outcome.turns,
        session_id=outcome.session_id,
        note="" if outcome.success else outcome.summary,
    )


async def _run_demo_session(workspace: Path, task: PlanTask, emit: Callable[[ImplEvent], None]) -> _Outcome:
    from idea_to_mvp.demo.implementer import demo_task

    await asyncio.sleep(0)
    return _Outcome(success=True, summary=demo_task(workspace, task, emit), turns=3)


async def _session_findings(watch: SessionWatch | None, label: str, *, keep: bool) -> list[IntegrityFinding]:
    """What a session did to the tests. A failed session in a worktree is thrown away with it (`keep=False`)."""
    if watch is None or not keep:
        return []
    return await asyncio.to_thread(watch.findings, session=label)


async def run_task(
    workspace: Path,
    task: PlanTask,
    settings: Settings,
    *,
    budget: BudgetTracker,
    emit: Callable[[ImplEvent], None],
    plan: Plan | None = None,
    position: tuple[int, int] | None = None,
    work_dir: Path | None = None,
    persist: bool = True,
    announce_end: bool = True,
    query_fn: Callable[..., AsyncIterator[Any]] | None = None,
) -> TaskResult:
    """Implement one task in a fresh session and record the result (a task already `done` is skipped).

    With `work_dir` (a git worktree) the session runs and its work is committed there, on the task's
    branch; progress is still read from `workspace`. The parallel engine passes `persist=False` and
    `announce_end=False` because a task only counts as done once its branch is merged, and the merge
    step records and announces that.
    """
    workspace = Path(workspace)
    progress = load_progress(workspace)
    previous = progress.get(task.id)
    if previous is not None and previous["status"] == "done":
        emit(make_event("task_end", task_id=task.id, label=task.title, detail="done (from an earlier run)"))
        return previous
    carried_cost = previous["cost_usd"] if previous is not None else 0.0  # spent on earlier failed attempts
    detail = f"{position[0]}/{position[1]}" if position else ""
    emit(make_event("task_start", task_id=task.id, label=task.title, detail=detail))

    remaining = budget.remaining()
    cwd = work_dir or workspace
    # The trajectory and the ledger row live in the main workspace even when the session runs in a worktree,
    # which is removed once the task is merged.
    log = SessionLog(
        workspace,
        kind="task",
        label=task.id,
        task_id=task.id,
        iteration=iteration_of(task.id),
        model=model_label(settings, "implementer"),
    )
    watch = None
    if remaining > 0:  # the test files as the session finds them
        watch = await asyncio.to_thread(SessionWatch, cwd, settings, session=task.id, iteration=iteration_of(task.id))
    if remaining <= 0:
        outcome = _Outcome(success=False, summary="Not started: the whole-run budget is used up.")
    elif settings.demo_mode:
        outcome = await _run_demo_session(cwd, task, log.tee(emit))
        _finish_log(log, outcome)
    else:
        outcome = await _run_session(
            cwd,
            task,
            settings,
            prompt=build_task_prompt(task, plan, progress, in_worktree=work_dir is not None),
            budget_usd=min(settings.implementer_max_task_usd, remaining),
            emit=emit,
            query_fn=query_fn,
            log=log,
        )
        _finish_log(log, outcome)
    budget.charge(outcome.cost_usd)
    findings = await _session_findings(watch, log.label, keep=outcome.success or work_dir is None)
    if work_dir is None and previous is not None:
        # A failed attempt's changes stay in the workspace (only a worktree is thrown away), so are its findings.
        findings = [*previous.get("integrity", []), *findings]  # type: ignore[list-item]

    commit = None
    if outcome.success and work_dir is not None:
        commit = await asyncio.to_thread(merge.commit_worktree, workspace, work_dir, task.id, f"{task.id}: {task.title}")
    elif outcome.success and await asyncio.to_thread(commit_workspace, workspace, f"{task.id}: {task.title}"):
        commit = head_commit(workspace)
    result: TaskResult = {
        "task_id": task.id,
        "status": "done" if outcome.success else "failed",
        "summary": outcome.summary,
        "cost_usd": carried_cost + outcome.cost_usd,
        "turns": outcome.turns,
        "session_id": outcome.session_id,
        "commit": commit,
    }
    if findings:
        result["integrity"] = [dict(finding) for finding in findings]
    if persist:
        save_result(workspace, result)
    if announce_end:
        detail = "done" if outcome.success else f"failed: {outcome.summary.splitlines()[0][:120]}"
        emit(make_event("task_end", task_id=task.id, label=task.title, detail=detail))
    return result


@dataclass
class ResolverOutcome:
    success: bool
    summary: str
    cost_usd: float = 0.0
    integrity: list[IntegrityFinding] = field(default_factory=list)  # what the resolver did to the tests


async def run_merge_resolver(
    workspace: Path,
    task: PlanTask,
    files: list[str],
    settings: Settings,
    *,
    emit: Callable[[ImplEvent], None],
    budget_usd: float,
    plan: Plan | None = None,
    query_fn: Callable[..., AsyncIterator[Any]] | None = None,
) -> ResolverOutcome:
    """One bounded agent session that resolves the conflict markers of a merge in progress in `workspace`.

    The orchestrator judges the result (no markers left, tests are the agent's job) and completes or aborts
    the merge; a crashing or failing session is reported as a failed resolution, never raised. The test files
    are compared before and after the session; the conflicted ones may lose assertions (a side had to win),
    but no test file may disappear or gain skip markers.
    """
    if settings.demo_mode:
        return ResolverOutcome(success=True, summary="Demo mode: nothing to resolve.")
    test_command = plan.commands.test if plan is not None else ""
    prompt = RESOLVER_PROMPT.format(
        task_id=task.id,
        title=task.title,
        files=_bullets([f"`{name}`" for name in files]),
        goal=task.goal,
        scope=task.scope,
        test_hint=f" (`{test_command}`)" if test_command.strip() else "",
    )
    log = SessionLog(
        Path(workspace),
        kind="resolver",
        label=f"{task.id}-resolver",
        task_id=task.id,
        iteration=iteration_of(task.id),
        model=model_label(settings, "escalation"),
    )
    watch = await asyncio.to_thread(
        SessionWatch, Path(workspace), settings, session=f"{task.id}-resolver", iteration=iteration_of(task.id)
    )
    outcome = await _run_session(
        Path(workspace),
        task,
        settings,
        prompt=prompt,
        budget_usd=budget_usd,
        emit=emit,
        query_fn=query_fn,
        role="escalation",  # a conflict between two finished tasks is a hard case
        log=log,
    )
    _finish_log(log, outcome)
    findings = await asyncio.to_thread(watch.findings, session=log.label, exempt=set(files))
    return ResolverOutcome(success=outcome.success, summary=outcome.summary, cost_usd=outcome.cost_usd, integrity=findings)


@dataclass
class PilotRollout:
    """One rollout of harness tuning's pilot task (`nodes/harness_tuning.py`)."""

    success: bool
    summary: str
    cost_usd: float = 0.0
    turns: int = 0
    crashed: bool = False
    commit: str | None = None  # the work, committed on the rollout's throwaway branch; None when nothing changed
    changed: str = ""  # `git diff --stat` of that commit
    trajectory: str = ""  # relative to the workspace
    integrity: list[IntegrityFinding] = field(default_factory=list)  # what the session did to the tests


async def run_pilot_rollout(
    workspace: Path,
    work_dir: Path,
    task: PlanTask,
    settings: Settings,
    *,
    arm: str,
    budget_usd: float,
    emit: Callable[[ImplEvent], None],
    plan: Plan | None = None,
    query_fn: Callable[..., AsyncIterator[Any]] | None = None,
) -> PilotRollout:
    """Build the pilot task in its own worktree (`work_dir`, made by the caller with the harness of this `arm` in
    place) as a task session would, and commit the work on the worktree's branch. Recorded as a `pilot` session
    (`pilot-<arm>`) in the workspace's ledger; nothing is recorded in `progress.json`, since the worktree is
    thrown away. A crashing session is a failed rollout, never raised (a missing sandbox still is). The test files
    are watched as for a task session, since the build may be selected as the task's result."""
    workspace, work_dir = Path(workspace), Path(work_dir)
    name = f"pilot-{arm}"
    log = SessionLog(workspace, kind="pilot", label=name, task_id=task.id, attempt=1, model=model_label(settings, "implementer"))
    watch = await asyncio.to_thread(SessionWatch, work_dir, settings, session=name, iteration=iteration_of(task.id))
    if settings.demo_mode:
        outcome = await _run_demo_session(work_dir, task, log.tee(emit))
    else:
        outcome = await _run_session(
            work_dir,
            task,
            settings,
            prompt=build_task_prompt(task, plan, load_progress(workspace), in_worktree=True),
            budget_usd=budget_usd,
            emit=emit,
            query_fn=query_fn,
            log=log,
        )
    _finish_log(log, outcome)
    commit = None
    if outcome.success:
        commit = await asyncio.to_thread(merge.commit_worktree, workspace, work_dir, name, f"pilot {task.id} ({arm} harness)")
    changed = await asyncio.to_thread(diff_stat, workspace, commit) if commit else ""
    trajectory = log.path.relative_to(workspace).as_posix() if log.path is not None else ""
    findings = await _session_findings(watch, name, keep=commit is not None)
    return PilotRollout(
        success=outcome.success,
        summary=outcome.summary,
        cost_usd=outcome.cost_usd,
        turns=outcome.turns,
        crashed=outcome.crashed,
        commit=commit,
        changed=changed,
        trajectory=trajectory,
        integrity=findings,
    )


def plan_is_runnable(plan: Plan | None) -> bool:
    """Whether the engine can schedule this plan: tasks exist, ids are unique, dependencies are known and acyclic."""
    if plan is None or not plan.tasks:
        return False
    ids = [task.id for task in plan.tasks]
    if len(set(ids)) != len(ids) or any(dep not in ids for task in plan.tasks for dep in task.depends_on):
        return False
    try:
        execution_waves(plan)
    except ValueError:
        return False
    return True


async def run_plan(
    workspace: Path,
    plan: Plan,
    settings: Settings,
    *,
    emit: Callable[[ImplEvent], None],
    budget: BudgetTracker | None = None,
    iteration: int | None = None,
    query_fn: Callable[..., AsyncIterator[Any]] | None = None,
) -> list[TaskResult]:
    """Run every task of the plan in dependency order, one session at a time.

    What earlier runs of this workspace already spent counts against the whole-run budget (with
    `iteration`, only what that version's tasks spent: every version has its own budget); `done` tasks
    are not run again; a task whose dependency did not finish is skipped.
    """
    workspace = Path(workspace)
    progress = load_progress(workspace)
    spent = (
        sum(result["cost_usd"] for result in progress.values())
        if iteration is None
        else spent_in_iteration(progress, iteration)
    )
    tuning = tuning_spend(workspace, iteration or 1)  # harness tuning's pilot sessions came out of the same budget
    if budget is None:
        budget = BudgetTracker(max(0.0, settings.implementer_max_total_usd - spent - tuning))
    if spent > 0:
        emit(make_event("cost", label="earlier runs", detail="spent before this run", cost_usd=spent))
    # No cost event for the tuning: its sessions emitted their own on this thread's stream (or the console replays
    # them from their ledger rows), so another would count them twice.
    tasks = {task.id: task for task in plan.tasks}
    order = [task_id for wave in execution_waves(plan) for task_id in wave]
    results: dict[str, TaskResult] = {}
    for index, task_id in enumerate(order, start=1):
        task = tasks[task_id]
        blocked = [dep for dep in task.depends_on if results[dep]["status"] != "done"]
        if blocked:
            result: TaskResult = {
                "task_id": task_id,
                "status": "skipped",
                "summary": f"Skipped: depends on unfinished task(s) {', '.join(blocked)}.",
                "cost_usd": 0.0,
                "turns": 0,
                "session_id": None,
                "commit": None,
            }
            save_result(workspace, result)
            emit(make_event("task_end", task_id=task_id, label=task.title, detail=f"skipped: waiting on {', '.join(blocked)}"))
        else:
            result = await run_task(
                workspace, task, settings, budget=budget, emit=emit, plan=plan, position=(index, len(order)), query_fn=query_fn
            )
        results[task_id] = result
    return list(results.values())


def render_task_log(plan: Plan, results: list[TaskResult]) -> str:
    """Markdown implementation log: one block per task with its status, cost, and the agent's summary."""
    titles = {task.id: task.title for task in plan.tasks}
    blocks = []
    for result in results:
        facts = [f"${result['cost_usd']:.2f}", f"{result['turns']} turns"]
        if result["commit"]:
            facts.append(f"commit {result['commit']}")
        blocks.append(
            f"## {result['task_id']} — {titles.get(result['task_id'], '')}: {result['status']}\n"
            f"{' · '.join(facts)}\n\n{result['summary'].strip() or 'No summary.'}"
        )
    return "\n\n".join(blocks)

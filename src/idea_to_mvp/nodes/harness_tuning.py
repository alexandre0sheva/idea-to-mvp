"""Harness tuning: Behavioral Contrast (Propose–Probe–Compose) on a pilot task, before the first version is built.

```
START → pilot ─Send×2→ propose → probe_wave ─Send×2→ probe ─┬→ compose ─┬→ adopt → END
          │                                                 └→ select ──┘
          └──── skipped (no git, no plan, no budget) ──────────────────────────────→ END
```

- `pilot` picks the first-wave task with the most dependents and builds it under the pack's harness in a throwaway
  worktree (the *base* run), then runs a quick tests lane there.
- `propose` (two calls in parallel, role `harness_editor`) reads the base run's trajectory and proposes an INV edit
  (how the agent works) and a DOM edit (what a practitioner of the stack knows), each checked by the harness
  validator; one repair attempt with the problems, else the candidate inherits the base harness.
- `probe` builds the pilot task again under each candidate, in parallel worktrees, each with its tests lane.
- `compose` (role `harness_composer`) reads the three runs and keeps the base, adopts one edit, or merges both.
  KEEP_BASE is a valid answer, an edit whose run delivered nothing is never adopted over a base run that delivered,
  and an answer that does not validate keeps the base.
- `select` (role `pilot_selector`, beside `compose`) compares the builds that delivered blind: shuffled with a seed
  and named A/B/C, shown as diffs and test results with the harness they ran under hidden (`pilot_selection.py`),
  scored on the judging rubric. A build whose tests did not pass, or whose session weakened the tests, is not
  selected over a clean one. Only when the plan is built task by task (a lead session takes no task's result).
- `adopt` writes the chosen harness into the workspace and commits it as `harness: tuned (pilot T0x)`, then applies
  the selected build on top as the pilot task's commit and records the task `done` in `progress.json`, so the
  engines do not build it again; then it removes the worktrees. It asserts the ledger holds exactly three rollouts
  (with a tests lane for each that delivered), two proposals, one composition, and the selection call when one was
  made; if it does not, the evidence is not trusted: the base harness is kept and no build is used.

The rollouts and lanes run on the whole-run budget, an equal share each of at most half of it
(`harness_tuning.session_budget`); the task engines deduct what they spent. The pure helpers are in
`idea_to_mvp/harness_tuning.py` and `idea_to_mvp/pilot_selection.py`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, TypedDict

from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Send

from idea_to_mvp.config import Settings, get_settings
from idea_to_mvp.harness_pack import read_harness, write_harness
from idea_to_mvp.harness_tuning import (
    EDIT_KINDS,
    MIN_SESSION_USD,
    TUNING_SHARE,
    Composition,
    Decision,
    HarnessEdit,
    HarnessFile,
    ProbeResult,
    TestsResult,
    adopted_changes,
    apply_edit,
    changes_of,
    compose_prompt,
    edit_problems,
    pick_pilot,
    proposal_prompt,
    repair_prompt,
    session_budget,
    tuning_identity,
    unsafe_choice,
)
from idea_to_mvp.implementation import executor, merge, verify
from idea_to_mvp.implementation.events import ImplEvent, make_event
from idea_to_mvp.implementation.ledger import (
    CALL_KINDS,
    CALL_USAGE_ROLES,
    TUNING_SESSION_KINDS,
    SessionKind,
    load_ledger,
    record_call,
)
from idea_to_mvp.implementation.progress import TaskResult, load_progress, save_result
from idea_to_mvp.implementation.trajectory import SessionLog, model_label
from idea_to_mvp.implementation.workspace import commit_diff, commit_workspace, head_commit
from idea_to_mvp.library import reference_block
from idea_to_mvp.nodes.common import structured_with_repair
from idea_to_mvp.nodes.implement import (
    agent_record,
    builds_task_by_task,
    emitter,
    route_after_workspace,
    runnable_plan,
)
from idea_to_mvp.pilot_selection import (
    SELECT_DIFF_CHARS,
    Selection,
    anonymize,
    build_section,
    candidates,
    overrule,
    selection_problems,
    selection_prompt,
)
from idea_to_mvp.plan import Plan, PlanTask
from idea_to_mvp.resilience import LLM_RETRY
from idea_to_mvp.roles import resolve_role_model
from idea_to_mvp.state import IdeaDiscussionState
from idea_to_mvp.text_utils import read_text
from idea_to_mvp.usage import UsageRecord, recording_usage

LOGGER = logging.getLogger(__name__)
_TOLERANCE_USD = 1e-6


class TuningInput(TypedDict):
    """What the parent hands the subgraph. Leaves out `usage` and `harness_tuning`, so the subgraph starts them empty
    and returns only what it produced (see `nodes/panel.py`)."""

    workspace_dir: str
    iteration: int
    execution_strategy: dict[
        str, Any
    ]  # whether the plan is built task by task: only then is a pilot build selected
    library: dict[
        str, Any
    ]  # the experience library read before the blueprint: the proposers get its index


class ProposeInput(TypedDict):
    """Payload of one `Send("propose", ...)`."""

    kind: str
    workspace_dir: str
    record: dict[str, Any]  # the tuning so far: the pilot and the base run
    reference: str  # the experience library's index as reference data ('' when there is none)


class ProbeInput(TypedDict):
    """Payload of one `Send("probe", ...)`."""

    kind: str
    workspace_dir: str
    record: dict[str, Any]
    candidate: dict[str, Any]


# ------------------------------------------------------------------ routing in the parent graph


def tuning_wanted(state: IdeaDiscussionState) -> bool:
    """Whether this run tunes the harness: only before the first version, once, and as chosen at the implement gate
    (the `HARNESS_TUNING` setting for a decision recorded before the choice existed)."""
    if int(state.get("iteration") or 1) != 1 or (state.get("harness_tuning") or {}).get("status"):
        return False
    choice = (state.get("implement_decision") or {}).get("harness_tuning")
    return bool(choice) if choice is not None else get_settings().harness_tuning == "pilot"


def route_after_acceptance(
    state: IdeaDiscussionState,
) -> Literal["harness_tuning", "implementer", "implement_plan"]:
    return "harness_tuning" if tuning_wanted(state) else route_after_workspace(state)


# ------------------------------------------------------------------ helpers


def _worktree_name(arm: str) -> str:
    return f"pilot-{arm}"


def _relabel(emit: Callable[[ImplEvent], None], task_id: str) -> Callable[[ImplEvent], None]:
    """Show a pilot session's events under `pilot:<arm>`, not under the plan task it builds (the board would think the
    task itself is running)."""
    return lambda event: emit({**event, "task_id": task_id})


def _call_model(settings: Settings, role: str) -> str:
    return "demo" if settings.demo_mode else resolve_role_model(settings, role)[1]


def _pilot_task(plan: Plan, task_id: str) -> PlanTask:
    return next(task for task in plan.tasks if task.id == task_id)


def _plan(workspace: Path) -> Plan:
    plan = runnable_plan(workspace)
    if plan is None:
        raise RuntimeError(f"{workspace} has no runnable plan.json")
    return plan


def _through_link(root: Path, path: str) -> bool:
    """Whether `root/path`, or a folder on the way to it, is a symbolic link."""
    parts = Path(path).parts
    return any(root.joinpath(*parts[: depth + 1]).is_symlink() for depth in range(len(parts)))


def _write_changes(root: Path, changes: dict[str, str]) -> list[str]:
    """Write an edit's files under `root` and remove the ones it empties; returns the paths changed. A path that is,
    or leads through, a symbolic link is skipped with a warning: writing there could land outside `root`."""
    linked = sorted(path for path in changes if _through_link(root, path))
    for path in linked:
        LOGGER.warning(
            "Harness tuning did not write %s: it is or leads through a symbolic link.", path
        )
    safe = {path: text for path, text in changes.items() if path not in linked}
    write_harness(root, {path: text for path, text in safe.items() if text.strip()})
    for path, text in safe.items():
        target = root / path
        if not text.strip() and target.is_file():
            target.unlink()
    return sorted(safe)


def _probes(record: dict[str, Any]) -> dict[str, ProbeResult]:
    return {"base": record["base"], **{kind: record[kind]["probe"] for kind in EDIT_KINDS}}


def _no_tests(reason: str) -> TestsResult:
    return {"ran": False, "passed": None, "failures": [], "summary": reason, "evidence": None}


async def _probe(
    workspace: Path,
    task: PlanTask,
    plan: Plan,
    settings: Settings,
    *,
    arm: str,
    changes: dict[str, str],
    budget_usd: float,
    emit: Callable[[ImplEvent], None],
) -> tuple[ProbeResult, list[UsageRecord]]:
    """One rollout: a fresh worktree from the workspace's HEAD with this arm's harness committed in it, the pilot task
    built there, and a quick tests lane when the session delivered changes. The worktree stays until `adopt`."""
    name = _worktree_name(arm)
    session_id, lane_id = f"pilot:{arm}", f"pilot:{arm}:tests"
    title = f"Pilot {task.id} ({arm} harness)"
    work_dir = await merge.create_worktree(workspace, name)
    if changes:
        # Committed apart, so the rollout's commit (and `delivered`) is the session's own work.
        written = _write_changes(work_dir, changes)
        candidate = await asyncio.to_thread(
            merge.commit_worktree, workspace, work_dir, name, f"harness: {arm} candidate"
        )
        if candidate is None or len(written) != len(changes):
            raise RuntimeError(
                f"could not lay the {arm} candidate harness down in {work_dir} (git commit failed or a path is a link)"
            )
    emit(make_event("task_start", task_id=session_id, label=title))
    rollout = await executor.run_pilot_rollout(
        workspace,
        work_dir,
        task,
        settings,
        arm=arm,
        budget_usd=budget_usd,
        emit=_relabel(emit, session_id),
        plan=plan,
    )
    usage = (
        [agent_record("pilot", model_label(settings, "implementer"), rollout.cost_usd)]
        if rollout.cost_usd > 0
        else []
    )
    delivered = rollout.success and rollout.commit is not None
    outcome = "crashed" if rollout.crashed else "success" if rollout.success else "failed"
    emit(
        make_event(
            "task_end",
            task_id=session_id,
            label=title,
            detail=outcome if delivered else f"{outcome}: delivered nothing",
        )
    )
    lane_cost = 0.0
    if delivered:
        log = SessionLog(
            workspace,
            kind="pilot_lane",
            label=f"{name}-tests",
            task_id=task.id,
            attempt=1,
            model=model_label(settings, "verifier"),
        )
        lane = await verify.run_lane(
            workspace,
            "tests",
            settings,
            budget_usd=budget_usd,
            emit=_relabel(emit, lane_id),
            plan=plan,
            work_dir=work_dir,
            log=log,
        )
        lane_cost = lane.cost_usd
        if lane_cost > 0:
            usage.append(agent_record("pilot", model_label(settings, "verifier"), lane_cost))
        report = lane.report
        tests: TestsResult = {
            "ran": True,
            "passed": report.passed,
            "failures": list(report.failures),
            "summary": report.summary,
            "evidence": report.evidence,
        }
    else:
        tests = _no_tests("the session delivered nothing to test")
    probe: ProbeResult = {
        "arm": arm,  # type: ignore[typeddict-item]
        "outcome": outcome,
        "delivered": delivered,
        "summary": rollout.summary,
        "cost_usd": rollout.cost_usd + lane_cost,
        "turns": rollout.turns,
        "commit": rollout.commit,
        "changed": rollout.changed,
        "tests": tests,
        "trajectory": rollout.trajectory,
    }
    if rollout.integrity:
        probe["integrity"] = [dict(finding) for finding in rollout.integrity]
    return probe, usage


def _record_call(
    workspace: Path,
    settings: Settings,
    *,
    kind: SessionKind,
    label: str,
    role: str,
    records: list[UsageRecord],
    started: float,
) -> None:
    cost = sum(record["cost_usd"] or 0.0 for record in records)
    record_call(
        workspace,
        kind=kind,
        label=label,
        model=_call_model(settings, role),
        cost_usd=cost,
        started_at=started,
        ended_at=time.time(),
    )


def _structured(
    role: str,
    messages: list[BaseMessage],
    schema: type[Any],
    fallback: Callable[[], Any],
    check: Callable[[Any], list[str]],
) -> tuple[Any, list[str]]:
    """A structured call checked by `check`, repaired once in the words of an edit (no files keeps the base)."""
    return structured_with_repair(role, messages, schema, fallback, check, repair=repair_prompt)


# ------------------------------------------------------------------ nodes


async def pilot_node(state: IdeaDiscussionState) -> dict[str, Any]:
    settings = get_settings()
    workspace = Path(state["workspace_dir"])
    emit = emitter()

    def skip(reason: str) -> dict[str, Any]:
        LOGGER.warning("Harness tuning skipped: %s.", reason)
        emit(make_event("text", label="harness tuning", detail=f"Skipped: {reason}."))
        return {"harness_tuning": {"status": "skipped", "note": f"Skipped: {reason}."}}

    if not (workspace / ".git").is_dir():
        return skip("the workspace is not a git repository, so the pilot runs have no worktrees")
    plan = runnable_plan(workspace)
    if plan is None:
        return skip("the plan cannot be scheduled")
    done = [
        task_id
        for task_id, result in load_progress(workspace).items()
        if result["status"] == "done"
    ]
    task = pick_pilot(plan, done)
    if task is None:
        return skip("no task of the plan is ready to start")
    budget = session_budget(settings)
    if budget < MIN_SESSION_USD:
        return skip(
            f"each pilot session could spend only ${budget:.2f} ({int(TUNING_SHARE * 100)}% of IMPLEMENTER_MAX_TOTAL_USD "
            f"shared by six sessions), less than the ${MIN_SESSION_USD:.2f} a task needs"
        )
    for arm in ("base", *EDIT_KINDS):  # left by a killed run
        await merge.remove_worktree(workspace, _worktree_name(arm))
    emit(
        make_event(
            "text",
            label="harness tuning",
            detail=f"Pilot task {task.id}: building it under the pack's harness",
        )
    )
    base, usage = await _probe(
        workspace, task, plan, settings, arm="base", changes={}, budget_usd=budget, emit=emit
    )
    record = {
        "status": "running",
        "pilot": {"id": task.id, "title": task.title},
        "budget_usd": budget,
        "base": base,
    }
    return {"harness_tuning": record, "usage": usage}


def route_after_pilot(state: IdeaDiscussionState) -> list[Send] | Literal["__end__"]:
    record = dict(state.get("harness_tuning") or {})
    if record.get("status") != "running":
        return "__end__"
    reference = reference_block(state.get("library"))
    return [
        Send(
            "propose",
            ProposeInput(
                kind=kind, workspace_dir=state["workspace_dir"], record=record, reference=reference
            ),
        )
        for kind in EDIT_KINDS
    ]


def _no_edit() -> HarnessEdit:
    return HarnessEdit(summary="", evidence=[], files=[])


def propose_node(state: ProposeInput) -> dict[str, Any]:
    settings = get_settings()
    workspace = Path(state["workspace_dir"])
    kind = state["kind"]
    record = state["record"]
    task = _pilot_task(_plan(workspace), record["pilot"]["id"])
    base_files = read_harness(workspace)
    base: ProbeResult = record["base"]
    trajectory = read_text(workspace / base["trajectory"]) if base["trajectory"] else ""
    started = time.time()
    with recording_usage("harness_editor") as records:
        edit, problems = _structured(
            "harness_editor",
            [
                HumanMessage(
                    content=proposal_prompt(
                        kind,  # type: ignore[arg-type]
                        task,
                        base_files,
                        base,
                        trajectory,
                        state.get("reference", ""),
                    )
                )
            ],
            HarnessEdit,
            _no_edit,
            lambda answer: edit_problems(base_files, answer.files),
        )
    _record_call(
        workspace,
        settings,
        kind="proposal",
        label=f"propose-{kind}",
        role="harness_editor",
        records=records,
        started=started,
    )
    if problems:
        LOGGER.warning(
            "The %s edit stays invalid (%s); its probe runs under the base harness.",
            kind.upper(),
            "; ".join(problems),
        )
    candidate = {
        "kind": kind,
        "summary": edit.summary.strip(),
        "evidence": [item.strip() for item in edit.evidence if item.strip()],
        "files": [] if problems else [item.model_dump() for item in edit.files],
        "inherited": bool(problems),
        "problems": problems,
    }
    return {"harness_tuning": {kind: candidate}, "usage": records}


def probe_wave_node(state: IdeaDiscussionState) -> dict[str, Any]:
    """A no-op the probes fan out from, once both proposals are in."""
    return {}


def route_probes(state: IdeaDiscussionState) -> list[Send]:
    record = dict(state.get("harness_tuning") or {})
    return [
        Send(
            "probe",
            ProbeInput(
                kind=kind,
                workspace_dir=state["workspace_dir"],
                record=record,
                candidate=dict(record[kind]),
            ),
        )
        for kind in EDIT_KINDS
    ]


async def probe_node(state: ProbeInput) -> dict[str, Any]:
    settings = get_settings()
    workspace = Path(state["workspace_dir"])
    kind, record, candidate = state["kind"], state["record"], state["candidate"]
    plan = _plan(workspace)
    task = _pilot_task(plan, record["pilot"]["id"])
    changes = changes_of(
        [HarnessFile.model_validate(item) for item in candidate.get("files") or []]
    )
    probe, usage = await _probe(
        workspace,
        task,
        plan,
        settings,
        arm=kind,
        changes=changes,
        budget_usd=float(record["budget_usd"]),
        emit=emitter(),
    )
    return {"harness_tuning": {kind: {**candidate, "probe": probe}}, "usage": usage}


def _keep_base() -> Composition:
    return Composition(
        decision="keep_base",
        files=[],
        notes=[],
        rationale="The composer gave no usable answer, so the base harness is kept.",
    )


def compose_node(state: IdeaDiscussionState) -> dict[str, Any]:
    settings = get_settings()
    workspace = Path(state["workspace_dir"])
    record = dict(state.get("harness_tuning") or {})
    task = _pilot_task(_plan(workspace), record["pilot"]["id"])
    base_files = read_harness(workspace)
    candidates: dict[str, Any] = {kind: record[kind] for kind in EDIT_KINDS}
    probes: dict[str, ProbeResult] = {
        "base": record["base"],
        **{kind: record[kind]["probe"] for kind in EDIT_KINDS},
    }
    trajectories = {
        arm: read_text(workspace / probe["trajectory"]) if probe["trajectory"] else ""
        for arm, probe in probes.items()
    }

    def check(answer: Composition) -> list[str]:
        return edit_problems(base_files, answer.files) if answer.decision == "merge" else []

    started = time.time()
    with recording_usage("harness_composer") as records:
        composition, problems = _structured(
            "harness_composer",
            [
                HumanMessage(
                    content=compose_prompt(task, base_files, candidates, probes, trajectories)
                )
            ],
            Composition,
            _keep_base,
            check,
        )
    _record_call(
        workspace,
        settings,
        kind="compose",
        label="compose",
        role="harness_composer",
        records=records,
        started=started,
    )
    proposed = composition.decision
    overruled = ""
    if problems:
        overruled = "the composed harness does not validate (" + "; ".join(problems) + ")"
    elif reason := unsafe_choice(proposed, probes):
        overruled = reason
    decision: Decision = "keep_base" if overruled else proposed
    changes = adopted_changes(decision, composition.files, candidates)
    if decision != "keep_base" and not changes:
        decision, overruled = "keep_base", "the adopted edit changes nothing"
    if overruled:
        LOGGER.warning(
            "The composer's %s is overruled: %s; keeping the base harness.", proposed, overruled
        )
    result = {
        "decision": decision,
        "proposed": proposed,
        "overruled": overruled,
        "notes": [note.model_dump() for note in composition.notes],
        "rationale": composition.rationale.strip(),
        "files": [item.model_dump() for item in changes],
    }
    return {"harness_tuning": {"compose": result}, "usage": records}


def _hidden(workspace: Path) -> list[str]:
    """What would tell the selector a build's harness: the pilot worktrees' paths (as given and resolved)."""
    paths = [merge.worktree_path(workspace, _worktree_name(arm)) for arm in ("base", *EDIT_KINDS)]
    return [str(path) for path in paths] + [os.path.realpath(path) for path in paths]


def _no_selection() -> Selection:
    return Selection(choice="none", scores=[], rationale="The selector gave no usable answer.")


def select_node(state: IdeaDiscussionState) -> dict[str, Any]:
    settings = get_settings()
    workspace = Path(state["workspace_dir"])
    record = dict(state.get("harness_tuning") or {})
    probes = _probes(record)

    def skip(note: str) -> dict[str, Any]:
        return {
            "harness_tuning": {"selection": {"status": "skipped", "note": note, "called": False}}
        }

    if not builds_task_by_task(state):
        return skip("the plan is built by one lead session, which does not take a task's result")
    arms = candidates(probes)
    if not arms:
        return skip("no pilot run delivered a build")
    task = _pilot_task(_plan(workspace), record["pilot"]["id"])
    # Seeded by what the runs produced, so a retried or resumed step shows the selector the same names.
    builds = anonymize(
        arms, "|".join([workspace.name, task.id, *(str(probes[arm]["commit"]) for arm in arms)])
    )
    sections = {
        build: build_section(
            build,
            probes[arm],
            commit_diff(workspace, probes[arm]["commit"] or "", 2 * SELECT_DIFF_CHARS),
        )
        for build, arm in builds.items()
    }
    prompt = selection_prompt(task, read_text(workspace / "PRD.md"), sections, _hidden(workspace))
    ids = list(builds)
    started = time.time()
    with recording_usage("pilot_selector") as records:
        answer, problems = _structured(
            "pilot_selector",
            [HumanMessage(content=prompt)],
            Selection,
            _no_selection,
            lambda reply: selection_problems(reply, ids),
        )
    _record_call(
        workspace,
        settings,
        kind="select",
        label="select",
        role="pilot_selector",
        records=records,
        started=started,
    )
    scores = {
        score.build.strip().upper(): score.model_dump()
        for score in answer.scores
        if score.build.strip().upper() in builds
    }
    choice, overruled = "none", ""
    if problems:
        overruled = (
            "the selector's answer stays unusable ("
            + "; ".join(problems)
            + "), so no build is selected"
        )
        LOGGER.warning("Pilot selection: %s.", overruled)
    else:
        choice, overruled = overrule(answer.choice, builds, probes, scores)
    if overruled and not problems:
        LOGGER.warning(
            "The selector's choice of build %s is overruled: %s.", answer.choice, overruled
        )
    selection = {
        "status": "chosen" if choice in builds else "none",
        "builds": builds,  # build id -> arm: revealed only now
        "proposed": answer.choice,
        "choice": choice if choice in builds else "",
        "arm": builds.get(choice, ""),
        "overruled": overruled,
        "scores": [{**scores[build], "build": build} for build in ids if build in scores],
        "rationale": answer.rationale.strip(),
        "called": True,
        "note": "",
    }
    return {"harness_tuning": {"selection": selection}, "usage": records}


async def _land_build(
    workspace: Path, task: PlanTask, selection: dict[str, Any], probe: ProbeResult
) -> dict[str, Any]:
    """Apply the selected build as the pilot task's commit and record the task done: the engines then skip it. Its
    cost stays harness tuning's (the task's result costs nothing more), so the budget does not count it twice."""
    previous = load_progress(workspace).get(task.id)
    if (
        previous is not None and previous["status"] == "done"
    ):  # a resumed step that had landed it already
        return {**selection, "landed": True, "task_commit": previous["commit"]}
    build, arm = selection["choice"], selection["arm"]
    message = f"{task.id}: {task.title} (pilot build {build}, selected)"
    outcome = await merge.apply_commit(workspace, probe["commit"] or "", message)
    if outcome.status != "merged":
        note = f"the selected build could not be applied ({outcome.detail}), so {task.id} is built again"
        LOGGER.warning("Pilot selection: %s.", note)
        return {**selection, "landed": False, "task_commit": None, "note": note}
    origin = "the base harness" if arm == "base" else f"the {str(arm).upper()} edit"
    summary = (
        f"Selected pilot build {build} of {len(selection['builds'])} (built under {origin} during harness tuning; its "
        f"${probe['cost_usd']:.2f} is counted there). {probe['summary']}"
    ).strip()
    result: TaskResult = {
        "task_id": task.id,
        "status": "done",
        "summary": summary,
        "cost_usd": 0.0,
        "turns": probe["turns"],
        "session_id": None,
        "commit": outcome.commit,
    }
    if probe.get("integrity"):
        result["integrity"] = list(probe.get("integrity") or [])
    save_result(workspace, result)
    emitter()(
        make_event(
            "task_end",
            task_id=task.id,
            label=task.title,
            detail=f"done (pilot build {build}, selected)",
        )
    )
    return {**selection, "landed": True, "task_commit": outcome.commit}


async def adopt_node(state: IdeaDiscussionState) -> dict[str, Any]:
    workspace = Path(state["workspace_dir"])
    record = dict(state.get("harness_tuning") or {})
    compose = dict(record.get("compose") or {})
    selection = dict(record.get("selection") or {})
    pilot = record["pilot"]["id"]
    probes = _probes(record)

    rows = [
        row
        for row in load_ledger(workspace)
        if row["iteration"] == 1 and row["kind"] in (*TUNING_SESSION_KINDS, *CALL_KINDS)
    ]
    delivered = sum(1 for probe in probes.values() if probe["delivered"])
    problems = tuning_identity(rows, delivered, 1 if selection.get("called") else 0)
    spent = sum(
        r["cost_usd"] or 0.0
        for r in state.get("usage") or []
        if r["role"] in ("pilot", *CALL_USAGE_ROLES)
    )
    recorded = sum(row["cost_usd"] for row in rows)
    if abs(spent - recorded) > _TOLERANCE_USD:
        problems.append(
            f"the ledger's tuning rows add up to ${recorded:.2f} but usage records ${spent:.2f}"
        )
    if problems:
        LOGGER.error(
            "Harness tuning's ledger does not add up (%s); keeping the base harness.",
            "; ".join(problems),
        )

    decision = compose.get("decision", "keep_base") if not problems else "keep_base"
    files = (
        [HarnessFile.model_validate(item) for item in compose.get("files") or []]
        if decision != "keep_base"
        else []
    )
    commit = None
    written: list[str] = []
    if files:
        base_files = read_harness(workspace)
        edited = apply_edit(base_files, files)
        written = _write_changes(
            workspace, {path: edited.get(path, "") for path in changes_of(files)}
        )
        committed = bool(written) and await asyncio.to_thread(
            commit_workspace, workspace, f"harness: tuned (pilot {pilot})"
        )
        commit = head_commit(workspace) if committed else None
    if selection.get("status") == "chosen":
        if problems:
            selection = {
                **selection,
                "landed": False,
                "task_commit": None,
                "note": f"the ledger did not add up, so no pilot build is used and {pilot} is built again",
            }
        else:
            task = _pilot_task(_plan(workspace), pilot)
            selection = await _land_build(workspace, task, selection, probes[selection["arm"]])
    for arm in (
        "base",
        *EDIT_KINDS,
    ):  # after the build landed: its commit lives on a worktree's branch
        await merge.remove_worktree(workspace, _worktree_name(arm))
    adopted = {
        "keep_base": "the base harness",
        "adopt_inv": "the INV edit",
        "adopt_dom": "the DOM edit",
        "merge": "both edits, merged",
    }
    landed = (
        f"; build {selection['choice']} is {pilot}'s result"
        if selection.get("landed")
        else f"; {pilot} is built again"
    )
    emit = emitter()
    emit(
        make_event(
            "text",
            label="harness tuning",
            detail=f"Pilot {pilot}: adopted {adopted.get(decision, decision)}{landed}",
        )
    )
    LOGGER.info("Harness tuning on %s: %s (%s).", pilot, decision, commit or "no commit")
    return {
        "harness_tuning": {
            "status": "done",
            "adopted": decision,
            "commit": commit,
            "changed_files": written,
            "selection": selection,
            "ledger": {
                "ok": not problems,
                "problems": problems,
                "rows": len(rows),
                "cost_usd": recorded,
            },
        }
    }


def build_tuning_subgraph() -> CompiledStateGraph:
    """The tuning stage, compiled to be mounted as the parent graph's `harness_tuning` node."""
    builder = StateGraph(IdeaDiscussionState, input_schema=TuningInput)
    builder.add_node("pilot", pilot_node)
    builder.add_node("propose", propose_node, retry_policy=LLM_RETRY)
    builder.add_node("probe_wave", probe_wave_node)
    builder.add_node("probe", probe_node)
    builder.add_node("compose", compose_node, retry_policy=LLM_RETRY)
    builder.add_node("select", select_node, retry_policy=LLM_RETRY)
    builder.add_node("adopt", adopt_node)
    builder.add_edge(START, "pilot")
    builder.add_conditional_edges("pilot", route_after_pilot, ["propose", END])
    builder.add_edge("propose", "probe_wave")
    builder.add_conditional_edges("probe_wave", route_probes, ["probe"])
    builder.add_edge("probe", "compose")
    builder.add_edge("probe", "select")
    builder.add_edge(["compose", "select"], "adopt")
    builder.add_edge("adopt", END)
    return builder.compile()

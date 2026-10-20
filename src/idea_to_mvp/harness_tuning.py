"""Behavioral Contrast on a pilot task (Propose–Probe–Compose): the pure helpers of `nodes/harness_tuning.py`.

The idea comes from Google Research's Harness² (arXiv:2610.14224, task-level recursion); the code and prompts are
this project's own. Before the first version is built, one pilot task is built under the pack's agent harness (the
*base* run). Two model calls read that run's trajectory and each propose one edit of the harness: **INV**, how the
agent works (workflow order, tool use, helper scripts, stop conditions), and **DOM**, what a practitioner of the stack
knows (skills, conventions, contracts). The pilot task is built again under each edit, and a composer reads the three
runs and keeps the base, adopts one edit, or merges both, judging only from behavioural contrasts between the
trajectories. Keeping the base is a valid answer.

Everything here is LLM-free and reads no project code: choosing the pilot, checking an edit against the harness
validator (`harness_pack.validate_harness`) and the rules of an edit, the prompts, the safety rule that an edit whose
run delivered nothing is not adopted over a base run that delivered, and the ledger identity the tuning asserts.
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal, TypedDict

from pydantic import BaseModel

from idea_to_mvp.harness_pack import (
    AGENTS_MD,
    BASE_GUARDRAILS,
    CHECK_SCRIPT,
    SETTINGS_PATH,
    in_surface,
    section_text,
    validate_harness,
)
from idea_to_mvp.plan import Plan, PlanTask, execution_waves

if TYPE_CHECKING:  # pragma: no cover - typing only
    from idea_to_mvp.config import Settings
    from idea_to_mvp.implementation.ledger import LedgerRow

Arm = Literal["base", "inv", "dom"]
EditKind = Literal["inv", "dom"]
Decision = Literal["keep_base", "adopt_inv", "adopt_dom", "merge"]
ARMS: tuple[Arm, ...] = ("base", "inv", "dom")
EDIT_KINDS: tuple[EditKind, ...] = ("inv", "dom")
KIND_TITLES: dict[EditKind, str] = {"inv": "INV (how the agent works)", "dom": "DOM (what a practitioner of this stack knows)"}
KIND_SCOPE: dict[EditKind, str] = {
    "inv": "workflow order, which tool to use for what, a helper script under `scripts/` for steps the agent kept doing "
    "by hand, when to stop",
    "dom": "a skill for a procedure the agent got wrong or rediscovered, the stack's conventions and pitfalls, how the "
    "contracts are meant to be used",
}

MAX_EDIT_FILES = 4
MAX_EDIT_FILE_CHARS = 20_000
PROTECTED_PATHS = (CHECK_SCRIPT, SETTINGS_PATH)  # the verdict's script and the fixed settings are the orchestrator's
# Three rollouts and up to three quick tests lanes share at most this part of the whole-run budget, in equal shares.
TUNING_SHARE = 0.5
TUNING_SESSIONS = 6
MIN_SESSION_USD = 0.25  # below this a rollout cannot do a task's work: the tuning is skipped
PROPOSAL_TRAJECTORY_CHARS = 24_000
COMPOSE_TRAJECTORY_CHARS = 12_000
LISTED_FILE_CHARS = 6_000
EXPECTED_CALLS = {"proposal": 2, "compose": 1}
EXPECTED_ROLLOUTS = 3


class HarnessFile(BaseModel):
    path: str  # relative to the project root, inside the harness surface
    content: str  # the file's complete new text; empty removes the file


class HarnessEdit(BaseModel):
    """What a harness editor proposes. An empty `files` list keeps the base harness."""

    summary: str  # one sentence: what the edit changes
    evidence: list[str]  # the trajectory moments that motivate it
    files: list[HarnessFile]


class ComposeNote(BaseModel):
    verdict: Literal["kept", "revised", "dropped"]
    edit: str  # which part of which edit (INV or DOM)
    evidence: str  # the trajectory moment or result that decided it


class Composition(BaseModel):
    """The composer's answer: the harness the project is built with, and why."""

    decision: Decision
    files: list[HarnessFile]  # `merge` only: the composed changes against the base harness
    notes: list[ComposeNote]
    rationale: str


class TestsResult(TypedDict):
    ran: bool
    passed: bool | None  # None when the lane did not run
    failures: list[str]
    summary: str
    evidence: dict[str, Any] | None  # what the orchestrator read from the test report


class ProbeResult(TypedDict):
    """One rollout of the pilot task under one harness."""

    arm: Arm
    outcome: str  # success / failed / crashed / not run
    delivered: bool  # the session succeeded and changed at least one file
    summary: str
    cost_usd: float  # the rollout session and its tests lane
    turns: int
    commit: str | None  # the rollout's commit on its throwaway branch
    changed: str  # `git diff --stat` of that commit
    tests: TestsResult
    trajectory: str  # the rollout's trajectory, relative to the workspace


# ------------------------------------------------------------------ the pilot


def pick_pilot(plan: Plan, done: Sequence[str] = ()) -> PlanTask | None:
    """The first-wave task (no unfinished dependency) with the most dependents, direct and indirect; ties go to the
    one with more direct dependents, then to plan order. None when the plan has nothing to start."""
    finished = set(done)
    try:
        waves = execution_waves(plan)
    except ValueError:
        return None
    first = [task_id for task_id in (waves[0] if waves else []) if task_id not in finished]
    if not first:
        return None
    direct: dict[str, set[str]] = {task.id: set() for task in plan.tasks}
    for task in plan.tasks:
        for dependency in task.depends_on:
            direct.setdefault(dependency, set()).add(task.id)

    def reach(task_id: str) -> set[str]:
        seen: set[str] = set()
        stack = list(direct.get(task_id, ()))
        while stack:
            current = stack.pop()
            if current not in seen:
                seen.add(current)
                stack += direct.get(current, ())
        return seen

    order = {task.id: index for index, task in enumerate(plan.tasks)}
    best = max(first, key=lambda task_id: (len(reach(task_id)), len(direct.get(task_id, ())), -order.get(task_id, 0)))
    return next(task for task in plan.tasks if task.id == best)


def session_budget(settings: Settings) -> float:
    """What one tuning session (a rollout or its tests lane) may spend: an equal share of the tuning's part of the
    whole-run budget, never more than a task may spend."""
    return min(settings.implementer_max_task_usd, settings.implementer_max_total_usd * TUNING_SHARE / TUNING_SESSIONS)


# ------------------------------------------------------------------ edits


def changes_of(files: Sequence[HarnessFile]) -> dict[str, str]:
    """An edit as path -> new text ('' removes the file); a later entry for the same path wins."""
    return {item.path.strip(): item.content for item in files}


def apply_edit(base: Mapping[str, str], files: Sequence[HarnessFile]) -> dict[str, str]:
    """The harness `base` becomes with the edit applied."""
    result = dict(base)
    for path, content in changes_of(files).items():
        if content.strip():
            result[path] = content
        else:
            result.pop(path, None)
    return result


def _guardrail_rules(files: Mapping[str, str]) -> str:
    return " ".join((section_text(files.get(AGENTS_MD, ""), "guardrails") or "").split())


def edit_problems(base: Mapping[str, str], files: Sequence[HarnessFile]) -> list[str]:
    """Why an edit cannot be applied to the harness `base`; [] when it can. Problems the base harness already has
    are not held against the edit."""
    problems: list[str] = []
    paths = [item.path.strip() for item in files]
    if len(set(paths)) > MAX_EDIT_FILES:
        problems.append(f"the edit changes {len(set(paths))} files (at most {MAX_EDIT_FILES})")
    for path in sorted(set(paths)):
        if path in PROTECTED_PATHS:
            problems.append(f"{path} belongs to the orchestrator and cannot be edited")
        elif not in_surface(path):
            problems.append(f"{path}: outside the harness surface")
    for path, content in changes_of(files).items():
        if len(content) > MAX_EDIT_FILE_CHARS:
            problems.append(f"{path} has {len(content)} characters (at most {MAX_EDIT_FILE_CHARS})")
        if not content.strip() and path not in base:
            problems.append(f"{path} does not exist, so it cannot be removed")
    if problems:
        return problems
    edited = apply_edit(base, files)
    known = set(validate_harness(base))
    problems += [problem for problem in validate_harness(edited) if problem not in known]
    rules = _guardrail_rules(edited)
    if section_text(base.get(AGENTS_MD, ""), "guardrails") is not None:
        problems += [f"the guardrails lost the rule: {rule}" for rule in BASE_GUARDRAILS if " ".join(rule.split()) not in rules]
    return problems


def edit_diff(base: Mapping[str, str], files: Sequence[HarnessFile]) -> str:
    """The edit as unified diffs against the base harness (for the composer)."""
    parts = []
    for path, content in sorted(changes_of(files).items()):
        before = base.get(path, "")
        after = content if content.strip() else ""
        diff = difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}" if path in base else "/dev/null",
            tofile=f"b/{path}" if after else "/dev/null",
        )
        parts.append("".join(line if line.endswith("\n") else line + "\n" for line in diff))
    return "".join(parts).strip() or "(no change)"


# ------------------------------------------------------------------ prompts


def fenced(text: str, info: str = "") -> str:
    """`text` in a code fence longer than any backtick run inside it, so quoted evidence cannot close the fence."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{info}\n{text.rstrip()}\n{fence}"


def excerpt(text: str, limit: int) -> str:
    """`text` cut to about `limit` characters, keeping its beginning and its end (where a session finishes)."""
    if len(text) <= limit:
        return text
    head = limit // 3
    tail = limit - head
    return f"{text[:head]}\n\n[… {len(text) - limit} characters cut …]\n\n{text[-tail:]}"


def harness_listing(files: Mapping[str, str]) -> str:
    """The current harness for a prompt: every file's text (long ones cut), the fixed files by name only."""
    parts = []
    for path in sorted(files):
        if path in PROTECTED_PATHS:
            parts.append(f"### {path}\n(fixed by the orchestrator; not editable)")
        else:
            parts.append(f"### {path}\n{fenced(excerpt(files[path], LISTED_FILE_CHARS))}")
    return "\n\n".join(parts) or "(no harness files)"


def task_brief(task: PlanTask) -> str:
    acceptance = "\n".join(f"- {item}" for item in task.acceptance) or "- (none listed)"
    return f"{task.id}: {task.title}\n- Goal: {task.goal}\n- Scope: {task.scope}\n- Acceptance criteria:\n{acceptance}"


def probe_lines(probe: ProbeResult) -> str:
    """What a rollout delivered, as a few lines for a prompt."""
    tests = probe["tests"]
    if not tests["ran"]:
        verdict = f"not run ({tests['summary']})" if tests["summary"] else "not run"
    else:
        evidence = tests.get("evidence") or {}
        counts = f" ({evidence.get('tests')} tests)" if evidence.get("tests") is not None else ""
        verdict = "passed" + counts if tests["passed"] else "failed" + counts
        if tests["failures"]:
            verdict += "\n" + "\n".join(f"  - {failure[:300]}" for failure in tests["failures"][:8])
    return (
        f"- Session: {probe['outcome']}, ${probe['cost_usd']:.2f}, {probe['turns']} turns; "
        f"{'delivered changes' if probe['delivered'] else 'delivered nothing'}\n"
        f"- Files changed:\n{fenced(probe['changed'] or '(none)')}\n"
        f"- Tests lane: {verdict}\n"
        f"- Its own summary: {probe['summary'][:600] or '(none)'}"
    )


def proposal_prompt(
    kind: EditKind, task: PlanTask, base_files: Mapping[str, str], base: ProbeResult, trajectory: str
) -> str:
    return (
        f"## Edit kind: {KIND_TITLES[kind]}\n"
        f"Propose one {kind.upper()} edit: {KIND_SCOPE[kind]}.\n\n"
        f"## The pilot task\n{task_brief(task)}\n\n"
        f"## What the base run delivered\n{probe_lines(base)}\n\n"
        f"## The current harness\n{harness_listing(base_files)}\n\n"
        f"## The base run's trajectory (`{base['trajectory'] or 'not recorded'}`)\n"
        f"{fenced(excerpt(trajectory, PROPOSAL_TRAJECTORY_CHARS) or '(empty)')}\n\n"
        f"Propose your {kind.upper()} edit now, or an empty `files` list if the trajectory points to no better harness."
    )


def repair_prompt(problems: Sequence[str], answer_json: str) -> str:
    listed = "\n".join(f"- {problem}" for problem in problems)
    return (
        f"Your answer cannot be applied:\n{listed}\n\nYour answer (JSON):\n{answer_json}\n\n"
        "Return it corrected: fix every problem and change nothing else, or return no files to keep the base."
    )


def _edit_section(kind: EditKind, candidate: Mapping[str, Any], base_files: Mapping[str, str]) -> str:
    files = [HarnessFile.model_validate(item) for item in candidate.get("files") or []]
    if candidate.get("inherited"):
        problems = "; ".join(str(problem) for problem in candidate.get("problems") or [])
        body = f"No usable edit: the editor's answer was invalid ({problems}), so this run used the base harness."
    elif not files:
        body = "No edit: the editor found nothing in the base trajectory to change, so this run used the base harness."
    else:
        evidence = "\n".join(f"- {item}" for item in candidate.get("evidence") or []) or "- (none given)"
        body = (
            f"Summary: {candidate.get('summary') or '(none)'}\n"
            f"Evidence the editor gave:\n{evidence}\n"
            f"Diff against the base harness:\n{fenced(edit_diff(base_files, files), 'diff')}"
        )
    return f"## The {KIND_TITLES[kind]} edit\n{body}"


def compose_prompt(
    task: PlanTask,
    base_files: Mapping[str, str],
    candidates: Mapping[str, Mapping[str, Any]],
    probes: Mapping[str, ProbeResult],
    trajectories: Mapping[str, str],
) -> str:
    runs = []
    for arm in ARMS:
        probe = probes[arm]
        name = "base harness" if arm == "base" else f"{arm.upper()} edit"
        runs.append(
            f"### Run under the {name}\n{probe_lines(probe)}\n"
            f"Trajectory (`{probe['trajectory'] or 'not recorded'}`):\n"
            f"{fenced(excerpt(trajectories.get(arm, ''), COMPOSE_TRAJECTORY_CHARS) or '(empty)')}"
        )
    edits = "\n\n".join(_edit_section(kind, candidates.get(kind) or {}, base_files) for kind in EDIT_KINDS)
    return (
        f"## The pilot task\n{task_brief(task)}\n\n{edits}\n\n## The three runs\n\n" + "\n\n".join(runs) + "\n\n"
        "Decide now: keep_base, adopt_inv, adopt_dom, or merge."
    )


# ------------------------------------------------------------------ composing


def unsafe_choice(decision: Decision, probes: Mapping[str, ProbeResult]) -> str:
    """Why `decision` may not stand: it adopts an edit whose run delivered nothing while the base run delivered.
    '' when it may."""
    used: list[EditKind] = {"adopt_inv": ["inv"], "adopt_dom": ["dom"], "merge": ["inv", "dom"]}.get(decision, [])  # type: ignore[assignment]
    empty = [kind for kind in used if not probes[kind]["delivered"]]
    if empty and probes["base"]["delivered"]:
        names = " and ".join(kind.upper() for kind in empty)
        return f"the run under the {names} edit delivered nothing while the base run delivered, so the edit is unsafe"
    return ""


def adopted_changes(
    decision: Decision, composed: Sequence[HarnessFile], candidates: Mapping[str, Mapping[str, Any]]
) -> list[HarnessFile]:
    """The edit the decision stands for: none, one candidate's, or the composed one."""
    if decision == "merge":
        return list(composed)
    if decision in ("adopt_inv", "adopt_dom"):
        kind = decision.removeprefix("adopt_")
        return [HarnessFile.model_validate(item) for item in (candidates.get(kind) or {}).get("files") or []]
    return []


# ------------------------------------------------------------------ ledger


def tuning_identity(rows: Sequence[LedgerRow], delivered: int) -> list[str]:
    """Whether the ledger holds exactly what one tuning makes: three rollouts, a quick tests lane for each one that
    delivered, two proposals, and one composition. [] when it does."""
    counts: Counter[str] = Counter(str(row["kind"]) for row in rows)
    expected = {"pilot": EXPECTED_ROLLOUTS, "pilot_lane": delivered, **EXPECTED_CALLS}
    return [
        f"the ledger has {counts.get(kind, 0)} {kind} row(s), expected {count}"
        for kind, count in expected.items()
        if counts.get(kind, 0) != count
    ]

"""Blind selection of harness tuning's pilot build: the pure helpers of `nodes/harness_tuning.py`'s `select` step.

Harness tuning builds its pilot task three times (under the base harness and under two edits of it). Rather than
throw the three builds away, a selector compares them and the chosen one becomes the pilot task's result, so the
engine does not build it a fourth time. The idea is Harness²'s blind selection (Google Research, arXiv:2610.14224);
the code and prompts are this project's own.

Blind means the selector cannot tell which harness produced which build: the builds that delivered are shuffled
with a seed (stable across a retried or resumed step) and named A, B, C; the selector sees each build's diff, its
tests-lane verdict, and what it did to the tests, never its trajectory, its own summary, its cost, or the harness
it ran under, and every text it does see has the pilot worktrees' names and paths taken out. It scores the builds
on the judging rubric (delivery, conformance, fidelity, consistency, operational fitness, finish) and picks one,
or none when no build is fit to be the task's result.

The orchestrator overrules a pick that the evidence contradicts: a build whose tests did not pass, or whose session
weakened the tests, is not selected over one that is clean on both; the best-scored clean build is taken instead.
Everything here is LLM-free.
"""

from __future__ import annotations

import random
import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel

from idea_to_mvp.harness_tuning import Arm, ProbeResult, excerpt, fenced, task_brief
from idea_to_mvp.plan import PlanTask

BUILD_IDS = ("A", "B", "C")
Choice = Literal["A", "B", "C", "none"]
CRITERIA = (
    "delivery",
    "conformance",
    "fidelity",
    "consistency",
    "operational_fitness",
    "finish",
)  # rubric order
SELECT_DIFF_CHARS = 16_000
SELECT_PRD_CHARS = 8_000
HIDDEN = "[hidden]"
_ARM_NAME = re.compile(r"pilot-(?:base|inv|dom)", re.IGNORECASE)


class BuildScore(BaseModel):
    build: str  # A, B, or C
    delivery: int  # 1 (worst) to 5 (best) on each criterion
    conformance: int
    fidelity: int
    consistency: int
    operational_fitness: int
    finish: int
    notes: str  # what decided the scores, in one or two sentences


class Selection(BaseModel):
    """The selector's answer: the build that becomes the pilot task's result, or `none`."""

    choice: Choice
    scores: list[BuildScore]
    rationale: str


def candidates(probes: Mapping[str, ProbeResult]) -> list[Arm]:
    """The arms whose build can be selected: the session succeeded and committed changes."""
    arms: tuple[Arm, ...] = ("base", "inv", "dom")
    return [
        arm for arm in arms if arm in probes and probes[arm]["delivered"] and probes[arm]["commit"]
    ]


def anonymize(arms: Sequence[Arm], seed: str) -> dict[str, Arm]:
    """Build id -> arm: the arms shuffled with `seed` and named A, B, C in that order."""
    shuffled = list(arms)
    random.Random(seed).shuffle(shuffled)
    return dict(zip(BUILD_IDS, shuffled, strict=False))


def redact(text: str, hidden: Sequence[str]) -> str:
    """`text` without the strings that would tell a build's harness (worktree paths, names), longest first."""
    for secret in sorted({item for item in hidden if item}, key=len, reverse=True):
        text = text.replace(secret, HIDDEN)
    return _ARM_NAME.sub(HIDDEN, text)


def _tests_line(probe: ProbeResult) -> str:
    tests = probe["tests"]
    if not tests["ran"]:
        return "not run"
    evidence = tests.get("evidence") or {}
    counts = f" ({evidence.get('tests')} tests)" if evidence.get("tests") is not None else ""
    line = ("passed" if tests["passed"] else "failed") + counts
    if tests["failures"]:
        line += "\n" + "\n".join(f"  - {failure[:300]}" for failure in tests["failures"][:8])
    return line


def _integrity_line(findings: Sequence[Mapping[str, Any]]) -> str:
    if not findings:
        return "no test was deleted, skipped, or weakened"
    return "\n" + "\n".join(
        f"  - {finding.get('kind', '')}: {finding.get('path', '')}" for finding in findings[:8]
    )


def build_section(build: str, probe: ProbeResult, diff: str) -> str:
    """One build for the prompt (not yet redacted): what it changed, its tests, what it did to the tests."""
    return (
        f"### Build {build}\n"
        f"- Files changed:\n{fenced(probe['changed'] or '(none)')}\n"
        f"- Tests lane: {_tests_line(probe)}\n"
        f"- Test integrity: {_integrity_line(probe.get('integrity') or [])}\n"
        f"- Diff:\n{fenced(excerpt(diff, SELECT_DIFF_CHARS) or '(empty)', 'diff')}"
    )


def selection_prompt(
    task: PlanTask, prd: str, sections: Mapping[str, str], hidden: Sequence[str]
) -> str:
    """The selector's prompt: the task, the PRD, and the builds by id, with every hidden string taken out."""
    builds = "\n\n".join(sections[build] for build in sorted(sections))
    names = ", ".join(sorted(sections))
    text = (
        f"## The task\n{task_brief(task)}\n\n"
        f"## PRD.md\n{fenced(excerpt(prd, SELECT_PRD_CHARS) or '(not found)')}\n\n"
        f"## The builds ({names})\n\n{builds}\n\n"
        f"Score every build ({names}) and choose one, or `none` if no build is fit to be this task's result."
    )
    return redact(text, hidden)


def selection_problems(answer: Selection, ids: Sequence[str]) -> list[str]:
    """Why an answer cannot be used: a choice that is not one of the builds, or a build scored not exactly once."""
    problems: list[str] = []
    if answer.choice != "none" and answer.choice not in ids:
        problems.append(
            f"`choice` is {answer.choice}, which is not one of the builds ({', '.join(ids)}) or none"
        )
    scored = [score.build.strip().upper() for score in answer.scores]
    for build in ids:
        if scored.count(build) != 1:
            problems.append(
                f"build {build} is scored {scored.count(build)} times (exactly once expected)"
            )
    if extra := sorted(set(scored) - set(ids)):
        problems.append(f"scores for builds that do not exist: {', '.join(extra)}")
    return problems


def score_key(score: Mapping[str, Any] | None) -> tuple[int, ...]:
    """A build's scores in rubric order, each clamped to 1..5 (an earlier criterion outweighs every later one)."""
    return tuple(max(1, min(5, int((score or {}).get(criterion) or 1))) for criterion in CRITERIA)


def _clean(probe: ProbeResult) -> tuple[bool, bool]:
    """(tests passed, no integrity finding): what a build must have before its scores count."""
    return bool(probe["tests"]["ran"] and probe["tests"]["passed"]), not probe.get("integrity")


def overrule(
    choice: str,
    builds: Mapping[str, Arm],
    probes: Mapping[str, ProbeResult],
    scores: Mapping[str, Mapping[str, Any]],
) -> tuple[str, str]:
    """(the build that stands, why the choice was overruled or ''). A chosen build whose tests did not pass, or
    whose session weakened the tests, gives way to the best-scored build that is cleaner on both (tests first);
    `none` stands."""
    if choice not in builds:
        return choice, ""
    tier = {build: _clean(probes[arm]) for build, arm in builds.items()}
    best = max(tier.values())
    if tier[choice] >= best:
        return choice, ""
    winner = max(
        (build for build in builds if tier[build] == best),
        key=lambda build: (score_key(scores.get(build)), -ord(build)),
    )
    reason = (
        "its tests did not pass"
        if best[0] and not tier[choice][0]
        else "its session weakened the tests"
    )
    return (
        winner,
        f"build {choice} was chosen but {reason}, while build {winner} is clean, so build {winner} is taken",
    )

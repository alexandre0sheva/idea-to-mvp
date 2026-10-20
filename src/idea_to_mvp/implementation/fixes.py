"""The contrastive fix loop: what a fix attempt leaves behind, and what the next one reads.

Attempt *N* of the fix loop must not start from the merged failure list alone, or it repeats attempt 1. After
every attempt the orchestrator writes `<workspace>/.idea-to-mvp/fix/attempt-<n>/` (git-ignored, read-only for
agents like the rest of that folder): the failures the attempt was given (`failures.md`), what it changed
(`diff.patch`), what it did (`trajectory.md`, a copy of its trajectory), and, once the next verification
round has been judged, the lane reports that followed (`after.md`). The next fix prompt points at the
folders instead of pasting them (evidence as files; Harness²'s sequential recursion) and requires a
`<notes>` block that judges each earlier hypothesis against that evidence.

This module also classifies failures: one that no change to the project can fix (a blocked domain, a missing
credential, a tool the sandbox cannot install) ends the loop with a clear report instead of burning attempts.
Everything here is LLM-free and reads files only; it never runs project code.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict, cast

from idea_to_mvp.implementation.ledger import LEDGER_DIR

FIX_DIR = "fix"
ENVIRONMENT_MARK = "ENVIRONMENT:"
_NO_CHANGES = "(no changes: this attempt changed no files)\n"
_TAG = re.compile(r"^\[(\w+)\]\s*")
_NOTES_BLOCK = re.compile(r"<notes>(.*?)</notes>", re.DOTALL | re.IGNORECASE)
_ENVIRONMENT_BLOCK = re.compile(r"<environment>(.*?)</environment>", re.DOTALL | re.IGNORECASE)
_DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/", re.MULTILINE)
_NOTE_VERDICTS = ("kept", "revised", "dropped", "new")
_JUDGING = ("kept", "revised", "dropped")  # verdicts on an earlier hypothesis
_HYPOTHESIS = ("new", "revised")  # verdicts that carry a hypothesis for this attempt
_LINE_LIMIT = 400
_REASON_LIMIT = 600

# A failure the orchestrator wrote from a test report (`<test id>: <assertion message>`): its words are the test's, in
# product language ("rejects missing credentials"), so only an explicit mark can make it an environment failure.
_TEST_CASE = re.compile(r"^(?:\S*::\S+|\S*\btest_\w+\S*|\S*Test\w*\S*):\s")
# A failure is an environment failure when the lane says so (`ENVIRONMENT:`) or when it describes something no change to
# the project can fix. A tool that is merely missing is not on this list: the project may have to declare it, and a
# fix session can say `<environment>` itself once it has tried.
_ENVIRONMENT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"could not resolve host|name or service not known|temporary failure in name resolution|\benotfound\b",
        r"(blocked|not allowed|denied|forbidden)[^.\n]{0,40}\b(domain|network|host|sandbox)\b",
        r"\b(domain|network access|host)\b[^.\n]{0,60}\b(is |was |are )?(blocked|not allowed|denied)\b",
        r"\bsandbox\b[^.\n]{0,40}\b(blocks?|blocked|denies|denied|does not allow)\b",
        r"\b(no|missing) (api[ _-]?key|credentials?|access token|auth(entication)? token)\b",
        r"\b(api[ _-]?key|credentials?|access token)\b[^.\n]{0,30}\b(is |are )?not (set|configured|available|provided)\b",
        r"\b(cannot|can't|could not|couldn't) be installed\b|\bnot (available|installed) (in|on) (the |this )?sandbox\b",
        r"\bsandbox\b[^.\n]{0,40}\b(has no|lacks|does not have|doesn't have)\b",
    )
)


# ---------------------------------------------------------------- failure classes


@dataclass(frozen=True)
class Classification:
    environment: list[str] = field(default_factory=list)  # failures no change to the project can fix
    code: list[str] = field(default_factory=list)  # everything else

    @property
    def only_environment(self) -> bool:
        """True when failures exist and every one is an environment failure: another fix attempt cannot help."""
        return bool(self.environment) and not self.code

    @property
    def reason(self) -> str:
        """The environment failures in one line each, without their lane tags, for the report."""
        lines = [_strip(failure)[:_LINE_LIMIT] for failure in self.environment]
        return "; ".join(dict.fromkeys(lines))[:_REASON_LIMIT]


def _strip(failure: str) -> str:
    """A failure without its `[lane]` tag and its `ENVIRONMENT:` mark."""
    text = _TAG.sub("", failure.strip())
    if text[: len(ENVIRONMENT_MARK)].upper() == ENVIRONMENT_MARK:
        text = text[len(ENVIRONMENT_MARK) :]
    return text.strip()


def _is_environment(failure: str) -> bool:
    tag = _TAG.match(failure.strip())
    if tag and tag.group(1) == "integrity":
        return False  # a test somebody weakened is always the project's to restore
    body = _TAG.sub("", failure.strip())
    if body[: len(ENVIRONMENT_MARK)].upper() == ENVIRONMENT_MARK:
        return True
    if (tag and tag.group(1) == "acceptance") or _TEST_CASE.match(body):
        return False  # a failing test's own name and message, never the lane's diagnosis
    return any(pattern.search(body) for pattern in _ENVIRONMENT_PATTERNS)


def classify_failures(failures: Sequence[str]) -> Classification:
    """Split a merged failure list into what the environment causes and what code can fix."""
    environment = [failure for failure in failures if _is_environment(failure)]
    code = [failure for failure in failures if not _is_environment(failure)]
    return Classification(environment, code)


# ---------------------------------------------------------------------- the notes


@dataclass(frozen=True)
class Note:
    """One line of a fix attempt's `<notes>`: a verdict on a hypothesis and the evidence that decided it."""

    verdict: str  # kept | revised | dropped | new
    hypothesis: str
    evidence: str

    def as_dict(self) -> dict[str, str]:
        return {"verdict": self.verdict, "hypothesis": self.hypothesis, "evidence": self.evidence}


def parse_notes(text: str) -> list[Note]:
    """The notes in an agent's final answer: `verdict -> hypothesis -> evidence`, one per line inside `<notes>`.

    A line in another shape is ignored (an attempt is never failed over its prose), see `notes_problem`.
    """
    found = _NOTES_BLOCK.search(text or "")
    if not found:
        return []
    notes: list[Note] = []
    for raw in found.group(1).splitlines():
        parts = [part.strip() for part in raw.strip().lstrip("-*• ").split("->")]
        if len(parts) < 3 or parts[0].lower() not in _NOTE_VERDICTS or not parts[1] or not parts[2]:
            continue
        notes.append(Note(parts[0].lower(), parts[1][:_LINE_LIMIT], " -> ".join(parts[2:])[:_LINE_LIMIT]))
    return notes


def parse_environment(text: str) -> str:
    """What the fix session says the environment lacks (`<environment>...</environment>`), '' when it says nothing."""
    found = _ENVIRONMENT_BLOCK.search(text or "")
    return " ".join(found.group(1).split())[:_REASON_LIMIT] if found else ""


def plain_answer(text: str) -> str:
    """An agent's final answer without its `<notes>` and `<environment>` blocks (what a person reads as the summary)."""
    return _ENVIRONMENT_BLOCK.sub("", _NOTES_BLOCK.sub("", text or "")).strip()


def notes_problem(notes: Sequence[Note], *, attempt: int, earlier: Sequence[Note]) -> str:
    """Why an attempt's notes do not meet the contract ('' when they do). A finding, never a reason to fail it.

    Every attempt owes a hypothesis (`new` or `revised`); one that follows notes of an earlier attempt must also
    judge them (`kept`, `revised`, or `dropped`).
    """
    if not notes:
        return f"fix attempt {attempt} left no <notes> block in the shape the prompt asks for."
    if earlier and not any(note.verdict in _JUDGING for note in notes):
        return f"fix attempt {attempt} did not judge any earlier hypothesis (kept, revised, or dropped)."
    if not any(note.verdict in _HYPOTHESIS for note in notes):
        return f"fix attempt {attempt} did not state a new hypothesis."
    return ""


# ------------------------------------------------------------- attempt folders


def attempt_name(iteration: int, attempt: int) -> str:
    """The folder of a fix attempt; a later version's attempts are prefixed so they stay apart from the first's."""
    return f"v{iteration}-attempt-{attempt}" if iteration > 1 else f"attempt-{attempt}"


def fix_root(workspace: Path) -> Path:
    return Path(workspace) / LEDGER_DIR / FIX_DIR


def write_attempt(
    workspace: Path,
    *,
    iteration: int,
    attempt: int,
    failures: Sequence[str],
    diff: str,
    trajectory: Path | None,
) -> Path:
    """Write what fix attempt `attempt` was given and did; returns its folder."""
    folder = fix_root(workspace) / attempt_name(iteration, attempt)
    folder.mkdir(parents=True, exist_ok=True)
    listing = "\n".join(f"- {failure}" for failure in failures) or "- (none listed)"
    (folder / "failures.md").write_text(f"# Failures fix attempt {attempt} was given\n\n{listing}\n", encoding="utf-8")
    (folder / "diff.patch").write_text(diff if diff.strip() else _NO_CHANGES, encoding="utf-8")
    target = folder / "trajectory.md"
    target.unlink(missing_ok=True)
    if trajectory is not None and Path(trajectory).is_file():
        shutil.copyfile(trajectory, target)
    return folder


def write_after(workspace: Path, *, iteration: int, attempt: int, passed: bool, report: str) -> None:
    """Add the lane reports that followed attempt `attempt` (the round the next verdict has just judged)."""
    folder = fix_root(workspace) / attempt_name(iteration, attempt)
    if not folder.is_dir():
        return
    outcome = "Verification passed after this attempt." if passed else "Verification was still failing after this attempt."
    (folder / "after.md").write_text(f"# After fix attempt {attempt}\n\n{outcome}\n\n{report.strip()}\n", encoding="utf-8")


def evidence_block(workspace: Path, *, iteration: int, attempt: int) -> str:
    """The part of the fix prompt that points at the earlier attempts of this version ('' for the first)."""
    folders = [
        name
        for earlier in range(1, attempt)
        if (fix_root(workspace) / (name := attempt_name(iteration, earlier))).is_dir()
    ]
    if not folders:
        return ""
    listing = "\n".join(f"- `{LEDGER_DIR}/{FIX_DIR}/{name}/`" for name in folders)
    return (
        "## Earlier fix attempts\n"
        f"This is fix attempt {attempt}. The attempts before it did not make verification pass. Each has a folder you "
        "can read (do not write there):\n"
        f"{listing}\n"
        "In each: `failures.md` (what it was given), `diff.patch` (what it changed), `trajectory.md` (what it did, "
        "command by command), and `after.md` (the lane reports of the round that followed it). Read them before you "
        "change anything. Do not repeat a change that is in a diff and did not help.\n"
        "In your `<notes>` block, give one line for every earlier hypothesis: `kept | revised | dropped -> the "
        "hypothesis -> the trajectory moment or report line that decided it`. The previous hypothesis failed, so "
        "end with a `new -> ...` line for the one you are testing now, unless a `revised` line already is it.\n"
    )


# --------------------------------------------------------------------- history


class FixAttempt(TypedDict):
    """One row of the delivery report's attempt history (kept in the state's `verification["history"]`)."""

    attempt: int
    model: str
    kind: str  # code | environment
    summary: str
    failures: list[str]  # what the attempt was given
    files: list[str]  # what it changed
    notes: list[dict[str, str]]
    notes_problem: str
    cost_usd: float
    result: str  # '' until the next round is judged, then passed | failed
    remaining: int  # failures left after it


def files_in(diff: str) -> list[str]:
    """The files a unified diff touches, in order."""
    return list(dict.fromkeys(_DIFF_HEADER.findall(diff or "")))


def history_entry(
    attempt: int,
    *,
    failures: Sequence[str],
    summary: str,
    files: Sequence[str],
    notes: Sequence[Note],
    model: str,
    kind: str = "code",
    problem: str = "",
    cost_usd: float = 0.0,
) -> FixAttempt:
    return {
        "attempt": attempt,
        "model": model,
        "kind": kind,
        "summary": summary[:_REASON_LIMIT],
        "failures": [failure[:_LINE_LIMIT] for failure in failures],
        "files": list(files),
        "notes": [note.as_dict() for note in notes],
        "notes_problem": problem,
        "cost_usd": cost_usd,
        "result": "",
        "remaining": 0,
    }


def close_entry(entry: Any, *, passed: bool, remaining: int) -> FixAttempt:
    """The entry of an attempt once the round after it is judged."""
    return cast(FixAttempt, {**entry, "result": "passed" if passed else "failed", "remaining": remaining})


_RESULT_TEXT = {"passed": "verification passed", "stopped": "stopped: nothing in the project could fix it"}


def result_text(entry: Any) -> str:
    """How an attempt ended, in words."""
    result = str(entry.get("result") or "")
    if result == "failed":
        return f"still failing ({int(entry.get('remaining') or 0)} failure(s) left)"
    return _RESULT_TEXT.get(result, "not verified")


def history_markdown(history: Sequence[Any]) -> str:
    """The attempt history as a markdown section for the delivery report ('' when there was no fix attempt)."""
    if not history:
        return ""
    blocks = ["### Fix attempts"]
    for entry in history:
        cost = f" · ${float(entry.get('cost_usd') or 0.0):.2f}" if entry.get("cost_usd") else ""
        lines = [f"**Attempt {entry.get('attempt')}** · {entry.get('model') or 'unknown model'}{cost} · {result_text(entry)}"]
        if files := entry.get("files") or []:
            lines.append("- Changed: " + ", ".join(f"`{name}`" for name in files))
        elif entry.get("kind") != "environment":
            lines.append("- Changed nothing.")
        lines += [
            f"- {note.get('verdict')}: {note.get('hypothesis')} (evidence: {note.get('evidence')})" for note in entry.get("notes") or []
        ]
        if entry.get("kind") == "environment":
            lines.append(f"- The session reported an environment problem: {entry.get('summary') or 'see the folder'}")
        if entry.get("notes_problem"):
            lines.append(f"- Note: {entry['notes_problem']}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)

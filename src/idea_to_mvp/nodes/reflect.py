"""After every delivery: what the run's evidence says, written into the stack's experience library (`library.py`).

```
delivery_report ─(EXPERIENCE_LIBRARY)→ reflect ─→ iterate_gate | END
                └──────── off ───────────────────↗
```

One structured call (role `reflector`) reads the evidence the run left (its ledger, the verification lanes, the test
integrity findings, the fix attempts and their notes, harness tuning's pilot runs, excerpts of trajectories) and
answers with edit rows for the harness edits the run tried and the library lessons it was given, and at most one
lesson. The evidence it reads leaves out the acceptance lane's failures, and the prompt is redacted (`library.redact`:
a fix session's notes can name a hidden test). What it answers is checked again before it is stored: a part that
quotes the acceptance suite or holds a secret is dropped, and `library.write_entry` refuses an entry that still would.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import HumanMessage

from idea_to_mvp.config import get_settings
from idea_to_mvp.harness_tuning import EDIT_KINDS, KIND_TITLES, excerpt, fenced, probe_lines
from idea_to_mvp.implementation.ledger import LedgerRow, load_ledger
from idea_to_mvp.library import (
    HARNESS_PREFIX,
    LESSON_PREFIX,
    Entry,
    LibraryLeak,
    Reflection,
    domain_for,
    entry_id,
    forbidden_for,
    no_entry,
    redact,
    reflection_problems,
    render_index,
    usable_lesson,
    usable_rows,
    value_leaks,
    write_entry,
)
from idea_to_mvp.library import load_entries as load_library
from idea_to_mvp.nodes.common import structured_with_repair
from idea_to_mvp.nodes.iterate import route_after_delivery
from idea_to_mvp.resilience import is_transient
from idea_to_mvp.state import IdeaDiscussionState
from idea_to_mvp.text_utils import clip, read_text
from idea_to_mvp.usage import with_usage

LOGGER = logging.getLogger(__name__)

TRAJECTORY_CHARS = 6_000  # per excerpt
TRAJECTORIES = 3  # excerpts per reflection
_FAILURES_LISTED = 8
_LINE_CHARS = 300
_ACCEPTANCE = "acceptance"


def route_to_reflect(state: IdeaDiscussionState) -> Literal["reflect", "iterate_gate", "__end__"]:
    """Reflect after a delivery while the library is on; otherwise straight on to the iterate gate (or the end)."""
    return "reflect" if get_settings().library_enabled else route_after_delivery(state)


# ------------------------------------------------------------------ the evidence


def _line(text: object) -> str:
    return clip(str(text), _LINE_CHARS)


def _not_acceptance(failures: Sequence[str]) -> list[str]:
    return [failure for failure in failures if not str(failure).startswith(f"[{_ACCEPTANCE}]")]


def ledger_section(rows: Sequence[LedgerRow]) -> str:
    lines = [
        f"- {row['label']} ({row['kind']}{', ' + row['task_id'] if row['task_id'] else ''}): {row['outcome']}, "
        f"${row['cost_usd']:.2f}, {row['turns']} turns, {row['model'] or 'model unknown'}"
        for row in rows
    ]
    return "\n".join(lines) or "- (no sessions recorded)"


def lanes_section(verification: Mapping[str, Any]) -> str:
    """Each lane's verdict and failures. The acceptance lane gives only its counts: its failures name held-out tests."""
    parts = []
    for lane in verification.get("lanes") or []:
        name = str(lane.get("lane") or "?")
        verdict = "passed" if lane.get("passed") else "failed"
        evidence = lane.get("evidence") or {}
        counts = (
            f" ({evidence.get('tests', 0)} tests, {evidence.get('failed', 0)} failed, {evidence.get('errors', 0)} errors)"
            if evidence
            else ""
        )
        if name == _ACCEPTANCE:
            parts.append(
                f"- acceptance: {verdict}{counts} (held-out tests; their names and messages are not shown)"
            )
            continue
        failures = "".join(
            f"\n  - {_line(failure)}" for failure in (lane.get("failures") or [])[:_FAILURES_LISTED]
        )
        parts.append(f"- {name}: {verdict}{counts}. {_line(lane.get('summary') or '')}{failures}")
    return "\n".join(parts) or "- (verification did not run)"


def integrity_section(findings: Mapping[str, Mapping[str, Any]], iteration: int) -> str:
    lines = [
        f"- {finding.get('session', '?')}: {finding.get('kind', '?')} in {finding.get('path', '?')} "
        f"({finding.get('status', '?')}): {_line(finding.get('detail') or '')}"
        for finding in findings.values()
        if int(finding.get("iteration") or 1) == iteration
    ]
    return "\n".join(lines) or "- (no session deleted, skipped, or weakened a test)"


def fixes_section(history: Sequence[Mapping[str, Any]]) -> str:
    parts = []
    for entry in history:
        failures = _not_acceptance(entry.get("failures") or [])
        given = "".join(f"\n  - given: {_line(failure)}" for failure in failures[:_FAILURES_LISTED])
        notes = "".join(
            f"\n  - note: {note.get('verdict', '?')} -> {_line(note.get('hypothesis') or '')} -> {_line(note.get('evidence') or '')}"
            for note in entry.get("notes") or []
        )
        files = ", ".join(entry.get("files") or []) or "none"
        parts.append(
            f"- attempt {entry.get('attempt')} on {entry.get('model') or '?'} ({entry.get('kind') or 'code'}): "
            f"{entry.get('result') or 'not judged'}; files changed: {files}{given}{notes}"
        )
    return "\n".join(parts) or "- (verification needed no fix attempt)"


def tuning_section(record: Mapping[str, Any]) -> str:
    if record.get("status") != "done":
        note = str(record.get("note") or "")
        return f"- (harness tuning did not run{': ' + note if note else ''})"
    compose = record.get("compose") or {}
    parts = [
        f"Pilot task {(record.get('pilot') or {}).get('id', '?')}; adopted: {record.get('adopted', '?')}; "
        f"composer rationale: {_line(compose.get('rationale') or '')}"
    ]
    parts += [
        f"- composer note: {note.get('verdict', '?')} {_line(note.get('edit') or '')}: {_line(note.get('evidence') or '')}"
        for note in compose.get("notes") or []
    ]
    for arm in ("base", *EDIT_KINDS):
        probe = (
            record.get("base") if arm == "base" else (record.get(arm) or {}).get("probe")
        ) or None
        if probe:
            title = "the base harness" if arm == "base" else KIND_TITLES[arm]  # type: ignore[index]
            parts.append(f"### Pilot run under {title}\n{probe_lines(probe)}")
    return "\n".join(parts)


def _excerpt_rows(rows: Sequence[LedgerRow]) -> list[LedgerRow]:
    """The sessions whose trajectories are worth reading: the ones that failed, then fix attempts and resolvers, then
    the longest task sessions."""
    with_file = [row for row in rows if row["trajectory"]]
    rank = {"crashed": 0, "failed": 0}
    kinds = {"fix": 1, "resolver": 1}

    def key(row: LedgerRow) -> tuple[int, int]:
        return (rank.get(row["outcome"], kinds.get(row["kind"], 2)), -row["turns"])

    return sorted(with_file, key=key)[:TRAJECTORIES]


def trajectories_section(workspace: Path, rows: Sequence[LedgerRow]) -> str:
    parts = []
    for row in _excerpt_rows(rows):
        text = read_text(workspace / row["trajectory"])
        if text.strip():
            parts.append(
                f"### {row['label']} ({row['kind']}, {row['outcome']})\n{fenced(excerpt(text, TRAJECTORY_CHARS))}"
            )
    return "\n\n".join(parts) or "(no trajectory recorded)"


def edits_to_judge(
    record: Mapping[str, Any], library: Mapping[str, Any], iteration: int
) -> dict[str, str]:
    """edit id -> what it was: harness tuning's edits that changed something, and the lessons the run was given. Only
    the first version tried them (later versions only build their change on top), so later reflections judge none."""
    if iteration != 1:
        return {}
    edits: dict[str, str] = {}
    if record.get("status") == "done":
        adopted = str(record.get("adopted") or "keep_base")
        for kind in EDIT_KINDS:
            candidate = record.get(kind) or {}
            if candidate.get("files"):
                used = adopted == "merge" or adopted == f"adopt_{kind}"
                edits[f"{HARNESS_PREFIX}{kind}"] = (
                    f"the {KIND_TITLES[kind]} edit of harness tuning: {_line(candidate.get('summary') or '')} "
                    f"({'adopted for the rest of the build' if used else 'tried on the pilot only, not adopted'})"
                )
    for lesson in library.get("lessons") or []:
        edits[f"{LESSON_PREFIX}{lesson.get('id')}"] = (
            f"the library lesson given to the planner and the harness editors: when {_line(lesson.get('cue') or '')}: "
            f"{_line(lesson.get('lesson') or '')}"
        )
    return edits


def reflection_prompt(
    state: IdeaDiscussionState, workspace: Path, domain: str, index: str, edits: Mapping[str, str]
) -> str:
    iteration = int(state.get("iteration") or 1)
    verification = state.get("verification") or {}
    rows = [row for row in load_ledger(workspace) if row["iteration"] == iteration]
    passed = "passed" if verification.get("passed") else "did not pass"
    judge = (
        "\n".join(f"- `{edit}`: {about}" for edit, about in edits.items())
        or "(none: return an empty `edits` list)"
    )
    return (
        f"## The delivery\nStack (library domain): `{domain}`. Version v0.{iteration}; verification {passed} after "
        f"{int(verification.get('attempts') or 0)} fix attempt(s).\n"
        f"Idea: {_line(state.get('user_idea') or '')}\n\n"
        f"## Sessions (the run ledger)\n{ledger_section(rows)}\n\n"
        f"## Verification lanes\n{lanes_section(verification)}\n\n"
        f"## Test integrity\n{integrity_section(state.get('integrity_findings') or {}, iteration)}\n\n"
        f"## Fix attempts\n{fixes_section(verification.get('history') or [])}\n\n"
        f"## Harness tuning\n{tuning_section((state.get('harness_tuning') or {}) if iteration == 1 else {})}\n\n"
        f"## Trajectory excerpts\n{trajectories_section(workspace, rows)}\n\n"
        f"## The library's current index (do not repeat what it already holds)\n{fenced(index.strip() or '(empty)', 'markdown')}\n\n"
        f"## Edits to judge\n{judge}\n\n"
        "Write the edit rows and at most one lesson now."
    )


# ------------------------------------------------------------------ the node


def _call(prompt: str, ids: list[str]) -> tuple[Reflection, list[str]]:
    return structured_with_repair(
        "reflector",
        [HumanMessage(content=prompt)],
        Reflection,
        no_entry,
        lambda answer: reflection_problems(answer, ids),
        keep_first=True,
    )


def _adopted_harness(
    record: Mapping[str, Any], iteration: int
) -> tuple[dict[str, Any] | None, dict[str, str]]:
    """The harness edit tuning adopted ({decision, summary, files}) and its files, or (None, {}) when it kept the
    base or did not run."""
    adopted = str(record.get("adopted") or "keep_base")
    if iteration != 1 or record.get("status") != "done" or adopted == "keep_base":
        return None, {}
    files = {
        str(item.get("path") or "").strip(): str(item.get("content") or "")
        for item in (record.get("compose") or {}).get("files") or []
        if str(item.get("content") or "").strip()
    }
    kinds = list(EDIT_KINDS) if adopted == "merge" else [adopted.removeprefix("adopt_")]
    summary = "; ".join(
        f"{kind.upper()}: {_line((record.get(kind) or {}).get('summary') or '')}" for kind in kinds
    )
    return {"decision": adopted, "summary": summary, "files": sorted(files)}, files


@with_usage(role="reflector")
def reflect_node(state: IdeaDiscussionState) -> dict[str, Any]:
    settings = get_settings()
    workspace = Path(state.get("workspace_dir") or "")
    iteration = int(state.get("iteration") or 1)
    if not state.get("workspace_dir") or not workspace.is_dir():
        return {"reflection": {"status": "skipped", "note": "There is no workspace to reflect on."}}
    library = dict(state.get("library") or {})
    domain = str(library.get("domain") or "") or domain_for(
        state.get("execution_strategy"),
        state.get("architecture_proposal"),
        state.get("arch_choice"),
    )
    root = settings.library_dir
    edits = edits_to_judge(state.get("harness_tuning") or {}, library, iteration)
    index = render_index(domain, load_library(root, domain))
    forbidden = forbidden_for(settings, workspace)
    try:
        # Fix sessions were told the acceptance failures, so their notes and trajectories can name the hidden tests.
        prompt = redact(reflection_prompt(state, workspace, domain, index, edits), forbidden)
        answer, problems = _call(prompt, list(edits))
    except Exception as exc:
        if is_transient(exc):
            raise  # retried with backoff; the run can be continued if it still fails
        LOGGER.warning(
            "The reflection failed, so the library gets nothing from this delivery: %s: %s",
            type(exc).__name__,
            exc,
        )
        return {
            "reflection": {
                "status": "failed",
                "domain": domain,
                "entry": "",
                "note": f"The reflection failed: {exc}.",
            }
        }
    if problems:
        LOGGER.warning(
            "The reflection still has problems (%s); keeping only its usable parts.",
            "; ".join(problems),
        )
    rows = usable_rows(answer, list(edits))
    lesson = usable_lesson(answer)
    dropped: list[str] = []
    if lesson is not None and (found := value_leaks(lesson.model_dump(), forbidden)):
        dropped.append(f"the lesson ({'; '.join(found)})")
        lesson = None
    rationale = answer.rationale.strip()
    if found := value_leaks(rationale, forbidden):
        dropped.append(f"the rationale ({'; '.join(found)})")
        rationale = ""
    kept = []
    for row in rows:
        if found := value_leaks(row.model_dump(), forbidden):
            dropped.append(f"the row on {row.edit} ({'; '.join(found)})")
        else:
            kept.append(row)
    harness, files = _adopted_harness(state.get("harness_tuning") or {}, iteration)
    if harness is not None and (value_leaks(files, forbidden) or value_leaks(harness, forbidden)):
        dropped.append("the adopted harness (a file holds acceptance-test content or a secret)")
        harness, files = None, {}
    for item in dropped:
        LOGGER.warning("The experience library did not record %s.", item)
    base = {
        "domain": domain,
        "lesson": lesson.model_dump() if lesson else None,
        "edits": [row.model_dump() for row in kept],
        "dropped": dropped,
        "rationale": rationale,
    }
    if lesson is None and not kept and harness is None:
        return {
            "reflection": {
                **base,
                "status": "no_entry",
                "entry": "",
                "note": "Nothing from this delivery to record.",
            }
        }
    entry: Entry = {
        "id": entry_id(workspace, iteration),
        "domain": domain,
        "project": workspace.name,
        "iteration": iteration,
        "verified": bool((state.get("verification") or {}).get("passed")),
        "created_at": time.time(),
        "edits": [row.model_dump() for row in kept],
        "lesson": lesson.model_dump() if lesson else None,
        "harness": harness,
    }
    try:
        write_entry(root, entry, files, forbidden=forbidden)
    except LibraryLeak as error:  # the checks above should have caught it: store nothing
        LOGGER.error("The experience library refused the entry: %s.", error)
        return {
            "reflection": {
                **base,
                "status": "refused",
                "entry": "",
                "note": f"Nothing was recorded: {error}.",
            }
        }
    except (OSError, ValueError) as error:
        LOGGER.exception("Could not write the experience library entry for %s", workspace)
        return {
            "reflection": {
                **base,
                "status": "refused",
                "entry": "",
                "note": f"Nothing was recorded: {error}.",
            }
        }
    return {"reflection": {**base, "status": "recorded", "entry": entry["id"], "note": ""}}

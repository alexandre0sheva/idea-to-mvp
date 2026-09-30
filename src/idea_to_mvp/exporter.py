"""Session exports: the whole run as Markdown (to read) and JSON (to process).

Both are built from the derived transcript (`ui/view.py`), so an export reproduces what the chat shows, from
the idea to the delivery report. The entries' typed `data` carries what text alone cannot: the blueprint's
file list and the verification lanes.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from idea_to_mvp.ui.view import ViewEntry

# Kinds that are a decision of the user's (not the idea or the answers, which have sections of their own).
_DECISION_KINDS = frozenset({"arch_decision", "planner_decision", "implement_decision", "change_request"})


def _slugify(text: str, *, max_length: int = 64) -> str:
    ascii_text = (text or "").encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text.lower()).strip("-")
    return (slug[:max_length].strip("-") or "session").strip("-") or "session"


def _normalize_questions(content: str) -> list[str]:
    questions: list[str] = []
    for raw_line in (content or "").splitlines():
        cleaned = re.sub(r"^\s*(?:\d+[\.\)]|[-*])\s*", "", raw_line).strip()
        if cleaned:
            questions.append(cleaned)
    return questions


def _append_section(lines: list[str], heading: str, body: str) -> None:
    text = (body or "").strip()
    if not text:
        return
    lines.extend([heading, "", text, ""])


def _numbered(heading: str, index: int) -> str:
    return heading if index == 1 else f"{heading} Revision {index}"


@dataclass
class _Run:
    """The entries of a session sorted by what they are."""

    idea: str = ""
    preferences: str = ""
    research: str = ""
    research_data: dict[str, Any] | None = None
    discussion: list[tuple[str, str]] = field(default_factory=list)
    moderator: list[str] = field(default_factory=list)
    summaries: list[str] = field(default_factory=list)
    questions: list[list[str]] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)
    architectures: list[str] = field(default_factory=list)
    strategy: str = ""
    decisions: list[str] = field(default_factory=list)
    blueprint_files: list[str] = field(default_factory=list)
    implementation: str = ""
    verification: dict[str, Any] | None = None
    delivery_report: str = ""
    other: list[tuple[str, str]] = field(default_factory=list)
    thinking: list[str] = field(default_factory=list)
    user_text: set[str] = field(default_factory=set)  # what the user already said (a draft repeating it is noise)


def _verification_of(data: object) -> dict[str, Any] | None:
    verification = data.get("verification") if isinstance(data, Mapping) and "verification" in data else data
    if isinstance(verification, Mapping) and (verification.get("lanes") or verification.get("report")):
        return dict(verification)
    return None


def _sort(entries: list[ViewEntry]) -> _Run:
    run = _Run()
    for entry in entries:
        kind, speaker, content = entry.kind.strip(), entry.speaker.strip() or "Assistant", entry.content.strip()
        if kind == "thinking":
            run.thinking.append(speaker)
        elif not content:
            continue
        elif kind == "idea" and not run.idea:
            run.idea = content
            run.user_text.add(content)
        elif kind == "preferences":
            run.preferences = content.removeprefix("Project preferences:").strip()
            run.user_text.add(content)
        elif kind == "research":
            run.research = content
            run.research_data = dict(entry.data) if isinstance(entry.data, Mapping) else None
        elif kind == "discussion":
            run.discussion.append((speaker, content))
        elif kind == "moderator":
            run.moderator.append(content)
        elif kind == "summary":
            run.summaries.append(content)
        elif kind == "questions":
            run.questions.append(_normalize_questions(content))
        elif kind == "user_answers":
            run.answers.append(content)
            run.user_text.add(content)
        elif kind == "architect":
            run.architectures.append(content)
        elif kind == "strategy":
            run.strategy = content
        elif kind == "project_bundle":
            run.blueprint_files = [str(name) for name in entry.data or []]
        elif kind == "implementation":
            run.implementation = content
        elif kind == "verification":
            run.verification = _verification_of(entry.data) or {"report": content}
        elif kind == "delivery_report":
            run.delivery_report = content
            run.verification = _verification_of(entry.data) or run.verification
        elif kind in _DECISION_KINDS:
            run.decisions.append(content)
            run.user_text.add(content)
        else:
            run.other.append((speaker, content))
    return run


# ---------------------------------------------------------------- markdown


def _verdict(verification: Mapping[str, Any]) -> str:
    verdict = "passed ✅" if verification.get("passed") else "failed ❌"
    return f"Verification {verdict} after {int(verification.get('attempts') or 0)} fix attempt(s)."


def _lane_markdown(lane: Mapping[str, Any]) -> str:
    rows = [f"### {lane.get('lane') or 'lane'} — {'passed ✅' if lane.get('passed') else 'failed ❌'}", ""]
    if summary := str(lane.get("summary") or "").strip():
        rows.extend([summary, ""])
    rows.extend(
        f"- `{run.get('command')}` → exit {run.get('exit_code')}" for run in lane.get("commands_run") or []
    )
    rows.extend(f"- ❌ {failure}" for failure in lane.get("failures") or [])
    return "\n".join(rows).rstrip()


def _verification_markdown(verification: Mapping[str, Any]) -> str:
    lanes = verification.get("lanes") or []
    if not lanes:  # it never ran: the report says why
        return str(verification.get("report") or "").strip()
    return "\n\n".join([_verdict(verification), *(_lane_markdown(lane) for lane in lanes)])


def build_session_markdown(
    *,
    entries: list[ViewEntry],
    thread_id: str,
    mode: str,
    draft_input: str = "",
    saved_at: datetime | None = None,
) -> str:
    timestamp = saved_at or datetime.now().astimezone()
    run = _sort(entries)

    lines = [
        "# Idea-to-MVP Session Export",
        "",
        f"- Saved: {timestamp.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"- Thread ID: `{thread_id or 'not-started'}`",
        f"- Current stage: `{mode or 'idea'}`",
        "",
    ]

    _append_section(lines, "## Idea", run.idea)
    _append_section(lines, "## Project Preferences", run.preferences)
    _append_section(lines, "## Research Brief", run.research)

    if run.discussion:
        lines.extend(["## Panel Discussion", ""])
        for idx, (speaker, content) in enumerate(run.discussion, start=1):
            lines.extend([f"### Turn {idx} - {speaker}", "", content, ""])
    for note in run.moderator:
        _append_section(lines, "## Moderator", note)

    for idx, summary in enumerate(run.summaries, start=1):
        _append_section(lines, _numbered("## Summary", idx), summary)

    for idx, questions in enumerate(run.questions, start=1):
        body = "\n".join(f"{n}. {question}" for n, question in enumerate(questions, start=1))
        _append_section(lines, _numbered("## MVP Questions", idx), body if questions else "")

    for idx, answer in enumerate(run.answers, start=1):
        _append_section(lines, _numbered("## Your Answers", idx), answer)

    for idx, architecture in enumerate(run.architectures, start=1):
        _append_section(lines, _numbered("## Architect Output", idx), architecture)

    _append_section(lines, "## Execution Strategy", run.strategy)
    if run.blueprint_files:
        _append_section(lines, "## Blueprint Pack", "\n".join(f"- `{name}`" for name in run.blueprint_files))
    _append_section(lines, "## Implementation", run.implementation)
    if run.verification:
        _append_section(lines, "## Verification", _verification_markdown(run.verification))
    _append_section(lines, "## Delivery Report", run.delivery_report)
    _append_section(lines, "## Your Decisions", "\n".join(f"- {decision}" for decision in run.decisions))

    for idx, (speaker, content) in enumerate(run.other, start=1):
        _append_section(lines, f"## {speaker}" if idx == 1 else f"## {speaker} {idx}", content)

    if run.thinking:
        lines.extend(["## Visible In-Progress Steps", ""])
        lines.extend(f"- {speaker} is still thinking." for speaker in run.thinking)
        lines.append("")

    draft = (draft_input or "").strip()
    if draft and draft not in run.user_text:
        _append_section(lines, "## Current Draft Input", draft)

    return "\n".join(lines).rstrip() + "\n"


# -------------------------------------------------------------------- json


def build_session_json(
    *,
    entries: list[ViewEntry],
    thread_id: str,
    mode: str,
    draft_input: str = "",
    saved_at: datetime | None = None,
) -> str:
    """The same run for programs: the transcript as it was shown, plus the typed parts lifted out."""
    timestamp = saved_at or datetime.now().astimezone()
    run = _sort(entries)
    document = {
        "saved_at": timestamp.isoformat(),
        "thread_id": thread_id,
        "stage": mode or "idea",
        "idea": run.idea,
        "preferences": run.preferences,
        "research": run.research_data,
        "draft_input": (draft_input or "").strip(),
        "blueprint_files": run.blueprint_files,
        "verification": run.verification,
        "delivery_report": run.delivery_report,
        "entries": [
            {"kind": entry.kind, "speaker": entry.speaker, "content": entry.content}
            for entry in entries
            if entry.kind != "thinking"
        ],
    }
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


# ------------------------------------------------------------------ saving


def save_session_export(
    *,
    exports_dir: Path,
    entries: list[ViewEntry],
    thread_id: str,
    mode: str,
    draft_input: str = "",
) -> tuple[Path, Path]:
    """Write the session as Markdown and JSON side by side; returns `(markdown_path, json_path)`."""
    timestamp = datetime.now().astimezone()
    options: dict[str, Any] = {
        "entries": entries,
        "thread_id": thread_id,
        "mode": mode,
        "draft_input": draft_input,
        "saved_at": timestamp,
    }
    seed_text = next(
        (
            entry.content.strip()
            for entry in entries
            if entry.kind.strip() == "idea" and entry.content.strip()
        ),
        (draft_input or "").strip(),
    )
    exports_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{timestamp.strftime('%Y%m%d-%H%M%S')}-{_slugify(seed_text)}"
    markdown_path, json_path = exports_dir / f"{stem}.md", exports_dir / f"{stem}.json"
    markdown_path.write_text(build_session_markdown(**options), encoding="utf-8")
    json_path.write_text(build_session_json(**options), encoding="utf-8")
    return markdown_path, json_path

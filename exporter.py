from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re


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


def build_session_markdown(
    *,
    transcript_state: list[dict[str, str]],
    thread_id: str,
    mode: str,
    draft_input: str = "",
    saved_at: datetime | None = None,
) -> str:
    timestamp = saved_at or datetime.now().astimezone()
    transcript = transcript_state or []

    idea = ""
    discussion_turns: list[dict[str, str]] = []
    summaries: list[str] = []
    question_sets: list[list[str]] = []
    answers: list[str] = []
    architectures: list[str] = []
    visible_thinking: list[str] = []
    other_entries: list[dict[str, str]] = []
    seen_user_text: set[str] = set()

    for entry in transcript:
        kind = (entry.get("kind") or "").strip()
        speaker = (entry.get("speaker") or "").strip() or "Assistant"
        content = (entry.get("content") or "").strip()
        if kind == "idea" and content and not idea:
            idea = content
            seen_user_text.add(content)
        elif kind == "discussion" and content:
            discussion_turns.append({"speaker": speaker, "content": content})
        elif kind == "summary" and content:
            summaries.append(content)
        elif kind == "questions" and content:
            question_sets.append(_normalize_questions(content))
        elif kind == "user_answers" and content:
            answers.append(content)
            seen_user_text.add(content)
        elif kind == "architect" and content:
            architectures.append(content)
        elif kind == "thinking":
            visible_thinking.append(speaker)
        elif content:
            other_entries.append({"speaker": speaker, "content": content})

    lines = [
        "# Idea-to-MVP Session Export",
        "",
        f"- Saved: {timestamp.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"- Thread ID: `{thread_id or 'not-started'}`",
        f"- Current stage: `{mode or 'idea'}`",
        "",
    ]

    _append_section(lines, "## Idea", idea)

    if discussion_turns:
        lines.extend(["## Panel Discussion", ""])
        for idx, turn in enumerate(discussion_turns, start=1):
            lines.extend(
                [
                    f"### Turn {idx} - {turn['speaker']}",
                    "",
                    turn["content"].strip(),
                    "",
                ]
            )

    for idx, summary in enumerate(summaries, start=1):
        heading = "## Summary" if idx == 1 else f"## Summary Revision {idx}"
        _append_section(lines, heading, summary)

    for idx, questions in enumerate(question_sets, start=1):
        if not questions:
            continue
        heading = "## MVP Questions" if idx == 1 else f"## MVP Questions Revision {idx}"
        body = "\n".join(f"{n}. {question}" for n, question in enumerate(questions, start=1))
        _append_section(lines, heading, body)

    for idx, answer in enumerate(answers, start=1):
        heading = "## Your Answers" if idx == 1 else f"## Your Answers Revision {idx}"
        _append_section(lines, heading, answer)

    for idx, architecture in enumerate(architectures, start=1):
        heading = "## Architect Output" if idx == 1 else f"## Architect Output Revision {idx}"
        _append_section(lines, heading, architecture)

    for idx, entry in enumerate(other_entries, start=1):
        heading = f"## {entry['speaker']}" if idx == 1 else f"## {entry['speaker']} {idx}"
        _append_section(lines, heading, entry["content"])

    if visible_thinking:
        lines.extend(["## Visible In-Progress Steps", ""])
        for speaker in visible_thinking:
            lines.append(f"- {speaker} is still thinking.")
        lines.append("")

    draft = (draft_input or "").strip()
    if draft and draft not in seen_user_text:
        _append_section(lines, "## Current Draft Input", draft)

    return "\n".join(lines).rstrip() + "\n"


def save_session_markdown(
    *,
    project_dir: Path,
    transcript_state: list[dict[str, str]],
    thread_id: str,
    mode: str,
    draft_input: str = "",
) -> Path:
    timestamp = datetime.now().astimezone()
    export_text = build_session_markdown(
        transcript_state=transcript_state,
        thread_id=thread_id,
        mode=mode,
        draft_input=draft_input,
        saved_at=timestamp,
    )
    seed_text = next(
        (
            (entry.get("content") or "").strip()
            for entry in transcript_state or []
            if (entry.get("kind") or "").strip() == "idea" and (entry.get("content") or "").strip()
        ),
        (draft_input or "").strip(),
    )
    exports_dir = project_dir / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    file_name = f"{timestamp.strftime('%Y%m%d-%H%M%S')}-{_slugify(seed_text)}.md"
    export_path = exports_dir / file_name
    export_path.write_text(export_text, encoding="utf-8")
    return export_path

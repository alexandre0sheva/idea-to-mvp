from __future__ import annotations

import html
import re
from collections.abc import Callable

render_markdown: Callable[..., str] | None
try:
    from markdown import markdown as render_markdown
except ImportError:  # pragma: no cover - optional runtime fallback
    render_markdown = None

SPEAKER_STYLE_CLASS: dict[str, str] = {
    "PM": "speaker-pm",
    "Tech Lead": "speaker-tech-lead",
    "Skeptic": "speaker-skeptic",
    "Summary": "speaker-summary",
    "Questions": "speaker-questions",
    "Architect": "speaker-architect",
    "Planner": "speaker-planner",
}


def _safe_markdown_to_html(content: str) -> str:
    text = (content or "").strip()
    if not text:
        text = "No visible response."
    escaped = html.escape(text)
    if render_markdown is None:
        return escaped.replace("\n", "<br>")
    return render_markdown(escaped, extensions=["extra", "sane_lists"])


def _style_class_for_speaker(speaker: str) -> str:
    return SPEAKER_STYLE_CLASS.get(speaker, "")


def thinking_block(speaker: str, message: str = "Analyzing constraints and MVP tradeoffs...") -> str:
    style_class = _style_class_for_speaker(speaker)
    return (
        f"<details class='speaker-card thinking {style_class}' open>"
        f"<summary class='speaker-head'>{speaker} · thinking</summary>"
        f"<div class='speaker-body'>{html.escape((message or '').strip() or 'Working...')}</div>"
        "</details>"
    )


def turn_block(speaker: str, content: str) -> str:
    body = _safe_markdown_to_html(content)
    style_class = _style_class_for_speaker(speaker)
    return (
        f"<details class='speaker-card {style_class}' open>"
        f"<summary class='speaker-head'>{speaker}</summary>"
        f"<div class='speaker-body'>{body}</div>"
        "</details>"
    )


def warning_block(title: str, content: str) -> str:
    """A red, always-open card for security warnings (shown with the implement gate)."""
    return (
        "<details class='speaker-card warning-card' open>"
        f"<summary class='speaker-head'>⚠️ {html.escape(title)}</summary>"
        f"<div class='speaker-body'>{html.escape((content or '').strip())}</div>"
        "</details>"
    )


def summary_block(summary: str) -> str:
    cleaned = re.sub(
        r"\n##\s*MVP\s*decision\s*questions[\s\S]*$",
        "",
        summary.strip(),
        flags=re.IGNORECASE,
    ).strip()
    safe = _safe_markdown_to_html(cleaned or "Summary was empty.")
    return (
        f"<details class='speaker-card summary {_style_class_for_speaker('Summary')}' open>"
        "<summary class='speaker-head'>Final summary</summary>"
        f"<div class='speaker-body'>{safe}</div>"
        "</details>"
    )


def architect_block(architecture_text: str) -> str:
    safe = _safe_markdown_to_html((architecture_text or "").strip() or "Architecture proposal was empty.")
    return (
        f"<details class='speaker-card summary {_style_class_for_speaker('Architect')}' open>"
        "<summary class='speaker-head'>Architect</summary>"
        f"<div class='speaker-body'>{safe}</div>"
        "</details>"
    )


def _normalize_question_text(q: str) -> str:
    text = re.sub(r"^\s*(?:\d+[\.\)]|[-*])\s*", "", (q or "").strip())
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    if not text.endswith("?"):
        text = text.rstrip(".") + "?"
    return text


def questions_block(questions: list[str], items: list[dict[str, str]] | None = None) -> str:
    """Render the MVP questions; typed `items` (schemas.MvpQuestion dumps) add why-it-matters + suggestion."""
    cleaned = [_normalize_question_text(q) for q in questions]
    cleaned = [q for q in cleaned if q][:5]
    if not cleaned:
        return ""
    details = items if items and len(items) == len(cleaned) else None
    rows: list[str] = []
    for index, question in enumerate(cleaned, start=1):
        row = f"{index}. {question}"
        if details:
            why = str(details[index - 1].get("why_it_matters") or "").strip()
            suggestion = str(details[index - 1].get("suggested_answer") or "").strip()
            if why:
                row += f"\n\n    *Why it matters:* {why}"
            if suggestion:
                row += f"\n\n    *Suggested answer:* {suggestion}"
        rows.append(row)
    body = _safe_markdown_to_html("\n\n".join(rows))
    return (
        f"<details class='speaker-card summary {_style_class_for_speaker('Questions')}' open>"
        "<summary class='speaker-head'>Questions before MVP build</summary>"
        f"<div class='speaker-body'>{body}</div></details>"
    )

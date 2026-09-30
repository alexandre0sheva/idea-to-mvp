from __future__ import annotations

import html
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from idea_to_mvp.schemas import ResearchBrief

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
    "Research": "speaker-research",
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


def turn_block(speaker: str, content: str, *, collapsed: bool = False, live: bool = False) -> str:
    """One speaker's card. `collapsed` closes it (older turns); `live` marks a turn that is still being written."""
    body = _safe_markdown_to_html(content)
    classes = " ".join(c for c in ("speaker-card", _style_class_for_speaker(speaker), "live-turn" if live else "") if c)
    head = html.escape(speaker) + (" · writing…" if live else "")
    return (
        f"<details class='{classes}'{'' if collapsed else ' open'}>"
        f"<summary class='speaker-head'>{head}</summary>"
        f"<div class='speaker-body'>{body}</div>"
        "</details>"
    )


def openings_row(
    turns: Sequence[tuple[str, str]], *, collapsed: bool = False, live: bool | Sequence[bool] = False
) -> str:
    """The panel's opening statements side by side (they were written at the same time, so they read as one).
    `live` says which cards are still being written: all of them, or one flag per card."""
    flags = [live] * len(turns) if isinstance(live, bool) else list(live)
    cards = "".join(
        turn_block(speaker, content, collapsed=collapsed, live=flag)
        for (speaker, content), flag in zip(turns, flags, strict=True)
    )
    return f"<div class='opening-row'>{cards}</div>"


def moderator_banner(reason: str) -> str:
    """The moderator's convergence note: a slim banner rather than a card, since it is a status, not a turn."""
    return (
        "<div class='moderator-banner'>🧭 <strong>Moderator</strong> · the panel converged: "
        f"{html.escape((reason or '').strip())}</div>"
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


def _link(url: str, label: str) -> str:
    """An external link that cannot hand the page's window to the target (the URL was validated as http(s))."""
    return f'<a href="{html.escape(url)}" target="_blank" rel="noopener noreferrer">{html.escape(label)}</a>'


def research_card(research: Mapping[str, Any]) -> str:
    """The research brief as a card. Everything in it came from the web: text is escaped, and only http(s)
    URLs become links (`ResearchBrief` has already dropped the rest)."""
    try:
        brief = ResearchBrief.model_validate(research)
    except ValidationError:
        return ""
    sections: list[str] = []
    if brief.competitors:
        items = "".join(
            f"<li>{_link(c.url, c.name)} — {html.escape(c.positioning)}"
            + (f" <em>Pricing: {html.escape(c.pricing)}</em>" if c.pricing else "")
            + "</li>"
            for c in brief.competitors
        )
        sections.append(f"<p><strong>Competitors</strong></p><ul>{items}</ul>")
    for title, notes in (("Market notes", brief.market_notes), ("Gaps", brief.gaps)):
        if notes:
            sections.append(f"<p><strong>{title}</strong></p><ul>{''.join(f'<li>{html.escape(n)}</li>' for n in notes)}</ul>")
    if brief.sources:
        sources = "".join(f"<li>{_link(url, url)}</li>" for url in brief.sources)
        sections.append(f"<p><strong>Sources</strong></p><ul>{sources}</ul>")
    return (
        f"<details class='speaker-card {_style_class_for_speaker('Research')}' open>"
        "<summary class='speaker-head'>Research brief</summary>"
        f"<div class='speaker-body'>{''.join(sections)}</div></details>"
    )

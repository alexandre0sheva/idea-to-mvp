from __future__ import annotations

import html
import re
from collections.abc import Callable

render_markdown: Callable[..., str] | None
try:
    from markdown import markdown as render_markdown
except ImportError:  # pragma: no cover - optional runtime fallback
    render_markdown = None

DEFAULT_IDEA = "I want to build a mobile learning app for photographers."
PANEL_CSS = """
.gradio-container { max-width: 100% !important; width: 100% !important; margin: 0 !important; padding: 0 12px !important; }
.speaker-card { border:1px solid #334155; border-radius:14px; padding:0.75rem 0.9rem; background:#111827; margin:0.25rem 0; }
.speaker-card summary { cursor:pointer; }
.speaker-card.thinking { background:#0f172a; border-color:#1d4ed8; }
.speaker-card.summary { border-color:#0e7490; background:#0c1f2b; }
.speaker-card.speaker-pm { border-color:#1f6f8b; background:#0b1a23; }
.speaker-card.speaker-tech-lead { border-color:#5b4b8a; background:#171228; }
.speaker-card.speaker-skeptic { border-color:#7a3e65; background:#231423; }
.speaker-card.speaker-summary { border-color:#2f7a50; background:#102218; }
.speaker-card.speaker-questions { border-color:#8a6a2b; background:#241d0d; }
.speaker-card.speaker-architect { border-color:#3a6ea5; background:#0d1a29; }
.speaker-card.speaker-planner { border-color:#0f766e; background:#102424; }
.speaker-head { color:#93c5fd; font-weight:700; margin-bottom:0.45rem; }
.speaker-card.speaker-pm .speaker-head { color:#7dd3fc; }
.speaker-card.speaker-tech-lead .speaker-head { color:#c4b5fd; }
.speaker-card.speaker-skeptic .speaker-head { color:#f9a8d4; }
.speaker-card.speaker-summary .speaker-head { color:#86efac; }
.speaker-card.speaker-questions .speaker-head { color:#fcd34d; }
.speaker-card.speaker-architect .speaker-head { color:#93c5fd; }
.speaker-card.speaker-planner .speaker-head { color:#5eead4; }
.speaker-body { color:#d1d5db; line-height:1.35; white-space:normal; }
.speaker-body p { margin: 0.1rem 0 0.3rem 0; }
.speaker-body p:last-child { margin-bottom: 0; }
.speaker-body ul, .speaker-body ol { margin: 0.2rem 0 0.3rem 1.2rem; padding-left: 0.2rem; }
.speaker-body li { margin: 0.04rem 0; }
.speaker-body h1, .speaker-body h2, .speaker-body h3, .speaker-body h4 { margin: 0.15rem 0 0.25rem 0; line-height: 1.25; }
.speaker-body code { background: rgba(255,255,255,0.08); padding: 0.08rem 0.3rem; border-radius: 5px; }
.stage-tracker { display:flex; flex-wrap:wrap; gap:0.35rem; margin:0.4rem 0 0.6rem 0; }
.stage-pill { border-radius:999px; padding:0.18rem 0.7rem; font-size:0.78rem; font-weight:600;
  border:1px solid #334155; color:#64748b; background:#0b1220; }
.stage-pill.done { border-color:#14532d; color:#86efac; background:#08160d; }
.stage-pill.active { border-color:#1d4ed8; color:#bfdbfe; background:#0b1a36; }
"""
PIPELINE_STAGES: list[tuple[str, str]] = [
    ("discussion", "Panel"),
    ("summary", "Summary"),
    ("answers", "Answers"),
    ("arch_choice", "Architecture"),
    ("strategy", "Strategy"),
    ("plan_bundle", "Blueprint"),
    ("implementation", "Implementation"),
    ("verification", "Verification"),
    ("done", "Done"),
]
_STAGE_ALIASES: dict[str, str] = {
    "architecture": "arch_choice",
    "plan_gate": "plan_bundle",
    "implement_gate": "implementation",
    "report": "verification",
}


def stage_tracker(stage: str) -> str:
    """Render the pipeline progress bar as a row of stage pills."""
    key = _STAGE_ALIASES.get((stage or "").strip(), (stage or "").strip())
    keys = [k for k, _ in PIPELINE_STAGES]
    index = keys.index(key) if key in keys else 0
    pills: list[str] = []
    for position, (_, label) in enumerate(PIPELINE_STAGES):
        if key == "done":
            css = "done" if position < len(PIPELINE_STAGES) - 1 else "active"
        elif position < index:
            css = "done"
        elif position == index:
            css = "active"
        else:
            css = "todo"
        pills.append(f"<span class='stage-pill {css}'>{html.escape(label)}</span>")
    return "<div class='stage-tracker'>" + "".join(pills) + "</div>"


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

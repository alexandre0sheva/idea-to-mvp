from __future__ import annotations

import html
import re

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
.speaker-head { color:#93c5fd; font-weight:700; margin-bottom:0.45rem; }
.speaker-card.speaker-pm .speaker-head { color:#7dd3fc; }
.speaker-card.speaker-tech-lead .speaker-head { color:#c4b5fd; }
.speaker-card.speaker-skeptic .speaker-head { color:#f9a8d4; }
.speaker-card.speaker-summary .speaker-head { color:#86efac; }
.speaker-card.speaker-questions .speaker-head { color:#fcd34d; }
.speaker-card.speaker-architect .speaker-head { color:#93c5fd; }
.speaker-body { color:#d1d5db; line-height:1.35; white-space:normal; }
.speaker-body p { margin: 0.1rem 0 0.3rem 0; }
.speaker-body p:last-child { margin-bottom: 0; }
.speaker-body ul, .speaker-body ol { margin: 0.2rem 0 0.3rem 1.2rem; padding-left: 0.2rem; }
.speaker-body li { margin: 0.04rem 0; }
.speaker-body h1, .speaker-body h2, .speaker-body h3, .speaker-body h4 { margin: 0.15rem 0 0.25rem 0; line-height: 1.25; }
.speaker-body code { background: rgba(255,255,255,0.08); padding: 0.08rem 0.3rem; border-radius: 5px; }
"""
SPEAKER_STYLE_CLASS: dict[str, str] = {
    "PM": "speaker-pm",
    "Tech Lead": "speaker-tech-lead",
    "Skeptic": "speaker-skeptic",
    "Summary": "speaker-summary",
    "Questions": "speaker-questions",
    "Architect": "speaker-architect",
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


def thinking_block(speaker: str) -> str:
    style_class = _style_class_for_speaker(speaker)
    return (
        f"<details class='speaker-card thinking {style_class}' open>"
        f"<summary class='speaker-head'>{speaker} · thinking</summary>"
        "<div class='speaker-body'>Analyzing constraints and MVP tradeoffs...</div>"
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


def questions_block(questions: list[str]) -> str:
    cleaned = [_normalize_question_text(q) for q in questions]
    cleaned = [q for q in cleaned if q][:5]
    if not cleaned:
        return ""
    lines = "\n".join(f"{i}. {q}" for i, q in enumerate(cleaned, start=1))
    body = _safe_markdown_to_html(lines)
    return (
        f"<details class='speaker-card summary {_style_class_for_speaker('Questions')}' open>"
        "<summary class='speaker-head'>Questions before MVP build</summary>"
        f"<div class='speaker-body'>{body}</div></details>"
    )

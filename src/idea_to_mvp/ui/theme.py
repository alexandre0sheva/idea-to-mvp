"""The look of the app: one Gradio theme plus the CSS for our own HTML (chat cards, stepper, badges).

Every colour is a CSS variable defined once for the light scheme (`:root`) and once for the dark scheme
(Gradio puts a `dark` class on the page); the rules below use only `var(--...)` and `color-mix()`, so a
card is tinted with its accent in both schemes. `tests/test_ui_render.py` enforces that no colour is
hard-coded outside the variable blocks and that text meets contrast targets in both schemes.
"""

from __future__ import annotations

import gradio as gr

CSS = """
:root {
  --page-bg: #f8fafc;
  --card-bg: #ffffff;
  --card-border: #cbd5e1;
  --text: #0f172a;
  --text-muted: #475569;
  --accent: #1d4ed8;
  --accent-pm: #0369a1;
  --accent-tech: #6d28d9;
  --accent-skeptic: #be185d;
  --accent-summary: #166534;
  --accent-questions: #92400e;
  --accent-architect: #1d4ed8;
  --accent-planner: #115e59;
  --ok: #166534;
  --warn: #92400e;
  --danger: #b91c1c;
  --card-accent: var(--card-border);
  --radius: 14px;
}

:root[data-theme='dark'], .dark {
  --page-bg: #0b1220;
  --card-bg: #111827;
  --card-border: #334155;
  --text: #e5e7eb;
  --text-muted: #9ca3af;
  --accent: #93c5fd;
  --accent-pm: #7dd3fc;
  --accent-tech: #c4b5fd;
  --accent-skeptic: #f9a8d4;
  --accent-summary: #86efac;
  --accent-questions: #fcd34d;
  --accent-architect: #93c5fd;
  --accent-planner: #5eead4;
  --ok: #86efac;
  --warn: #fcd34d;
  --danger: #fca5a5;
}

.gradio-container { max-width: 100% !important; width: 100% !important; margin: 0 !important; padding: 0 12px !important; }

/* Sticky progress bar above the chat: the stepper and the token / cost badges. */
.stage-header {
  position: sticky; top: 0; z-index: 20;
  display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 0.5rem 1rem;
  margin: 0.4rem 0 0.6rem 0; padding: 0.45rem 0.7rem;
  background: var(--card-bg); color: var(--text);
  border: 1px solid var(--card-border); border-radius: var(--radius);
}
.stage-stepper { display: flex; flex-wrap: wrap; gap: 0.35rem; margin: 0; padding: 0; list-style: none; }
.stage-step {
  display: flex; flex-direction: column; align-items: center; min-width: 4.6rem;
  padding: 0.15rem 0.7rem; border-radius: 999px;
  border: 1px solid var(--card-border); color: var(--text-muted); background: var(--card-bg);
  font-size: 0.78rem; font-weight: 600; line-height: 1.25;
}
.stage-step .stage-time { font-size: 0.68rem; font-weight: 500; }
.stage-step.done { color: var(--ok); border-color: var(--ok); background: color-mix(in srgb, var(--ok) 10%, var(--card-bg)); }
.stage-step.active { color: var(--accent); border-color: var(--accent); background: color-mix(in srgb, var(--accent) 12%, var(--card-bg)); }
.stage-step.failed { color: var(--danger); border-color: var(--danger); background: color-mix(in srgb, var(--danger) 10%, var(--card-bg)); }
.usage-badges { display: flex; gap: 0.35rem; }
.usage-badge {
  padding: 0.15rem 0.6rem; border-radius: 999px; font-size: 0.78rem; font-weight: 600;
  color: var(--text); border: 1px solid var(--card-border);
  background: color-mix(in srgb, var(--accent) 10%, var(--card-bg));
}

/* Chat cards. A speaker class sets the accent; the card border, tint and title follow it. */
.speaker-card {
  border: 1px solid var(--card-accent); border-radius: var(--radius); padding: 0.75rem 0.9rem; margin: 0.25rem 0;
  background: color-mix(in srgb, var(--card-accent) 10%, var(--card-bg)); color: var(--text);
}
.speaker-card summary { cursor: pointer; }
.speaker-card.thinking { border-style: dashed; }
.speaker-card.summary { --card-accent: var(--accent-summary); }
.speaker-card.speaker-pm { --card-accent: var(--accent-pm); }
.speaker-card.speaker-tech-lead { --card-accent: var(--accent-tech); }
.speaker-card.speaker-skeptic { --card-accent: var(--accent-skeptic); }
.speaker-card.speaker-summary { --card-accent: var(--accent-summary); }
.speaker-card.speaker-questions { --card-accent: var(--accent-questions); }
.speaker-card.speaker-architect { --card-accent: var(--accent-architect); }
.speaker-card.speaker-planner { --card-accent: var(--accent-planner); }
.speaker-card.warning-card { --card-accent: var(--danger); }
.speaker-head { color: var(--card-accent); font-weight: 700; margin-bottom: 0.45rem; }
.speaker-body { color: var(--text); line-height: 1.35; white-space: normal; }
.speaker-body p { margin: 0.1rem 0 0.3rem 0; }
.speaker-body p:last-child { margin-bottom: 0; }
.speaker-body ul, .speaker-body ol { margin: 0.2rem 0 0.3rem 1.2rem; padding-left: 0.2rem; }
.speaker-body li { margin: 0.04rem 0; }
.speaker-body h1, .speaker-body h2, .speaker-body h3, .speaker-body h4 { margin: 0.15rem 0 0.25rem 0; line-height: 1.25; }
.speaker-body code { background: color-mix(in srgb, var(--text) 10%, transparent); padding: 0.08rem 0.3rem; border-radius: 5px; }

/* Gate forms: the architecture comparison, the implement summary, banners. */
.gate-form { gap: 0.6rem; }
.arch-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 0.75rem; }
.arch-card {
  --card-accent: var(--accent-architect);
  border: 1px solid var(--card-accent); border-radius: var(--radius); padding: 0.75rem 0.9rem; color: var(--text);
  background: color-mix(in srgb, var(--card-accent) 8%, var(--card-bg));
}
.arch-card.recommended { --card-accent: var(--ok); border-width: 2px; }
.arch-card h4 { margin: 0 0 0.3rem 0; color: var(--card-accent); }
.arch-card ul { margin: 0.1rem 0 0.4rem 1.1rem; padding: 0; }
.arch-card p { margin: 0.2rem 0; }
.arch-style { color: var(--text-muted); }
.arch-why { margin: 0.4rem 0 0 0; color: var(--text); }
.chips { display: flex; flex-wrap: wrap; gap: 0.3rem; margin: 0.3rem 0; }
.chip {
  padding: 0.05rem 0.55rem; border-radius: 999px; font-size: 0.78rem; font-weight: 600; color: var(--text);
  border: 1px solid var(--card-border); background: color-mix(in srgb, var(--text) 6%, var(--card-bg));
}
.badge-recommended {
  margin-left: 0.4rem; padding: 0.05rem 0.5rem; border-radius: 999px; font-size: 0.72rem; font-weight: 700;
  color: var(--ok); border: 1px solid var(--ok); background: color-mix(in srgb, var(--ok) 12%, var(--card-bg));
}
.gate-table { width: 100%; border-collapse: collapse; color: var(--text); }
.gate-table th, .gate-table td { padding: 0.3rem 0.6rem; text-align: left; border-bottom: 1px solid var(--card-border); }
.gate-table th { width: 11rem; color: var(--text-muted); font-weight: 600; }
.gate-banner {
  padding: 0.6rem 0.8rem; border-radius: var(--radius); color: var(--text);
  border: 1px solid var(--danger); background: color-mix(in srgb, var(--danger) 10%, var(--card-bg));
}
.gate-banner.warn { border-color: var(--warn); background: color-mix(in srgb, var(--warn) 10%, var(--card-bg)); }
.gate-banner ul { margin: 0.2rem 0 0 1.1rem; padding: 0; }

/* Implementation view: cost meter, task board, live console. */
.cost-meter { display: flex; align-items: center; gap: 0.7rem; margin: 0.2rem 0 0.6rem 0; color: var(--text); }
.cost-bar { flex: 1; height: 0.6rem; border-radius: 999px; overflow: hidden; border: 1px solid var(--card-border); background: var(--card-bg); }
.cost-fill { height: 100%; background: var(--ok); }
.cost-meter.warn .cost-fill { background: var(--warn); }
.cost-meter.over .cost-fill { background: var(--danger); }
.cost-meter.over .cost-text { color: var(--danger); font-weight: 700; }
.cost-text { font-weight: 600; white-space: nowrap; }
.task-board { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 0.6rem; margin: 0.4rem 0; }
.board-col {
  padding: 0.5rem; border-radius: var(--radius); border: 1px solid var(--card-border);
  background: color-mix(in srgb, var(--text) 4%, var(--card-bg)); color: var(--text);
}
.board-col h4 { margin: 0 0 0.4rem 0; color: var(--text-muted); font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.04em; }
.task-card {
  --card-accent: var(--card-border);
  margin: 0.35rem 0; padding: 0.5rem 0.65rem; border-radius: 10px; border: 1px solid var(--card-accent); color: var(--text);
  background: color-mix(in srgb, var(--card-accent) 10%, var(--card-bg));
}
.task-card.running { --card-accent: var(--accent); }
.task-card.done { --card-accent: var(--ok); }
.task-card.failed { --card-accent: var(--danger); }
.task-card header { display: flex; gap: 0.4rem; align-items: baseline; flex-wrap: wrap; }
.task-title { font-weight: 600; }
.task-meta { display: flex; flex-wrap: wrap; gap: 0.3rem 0.5rem; align-items: center; margin-top: 0.25rem; font-size: 0.78rem; color: var(--text-muted); }
.task-card details { margin-top: 0.3rem; }
.task-card summary { cursor: pointer; color: var(--text-muted); font-size: 0.8rem; }
.task-summary { margin: 0.3rem 0; font-size: 0.85rem; }
.diffstat, .run-block {
  margin: 0.3rem 0; padding: 0.4rem 0.5rem; overflow-x: auto; font-size: 0.78rem; border-radius: 8px;
  color: var(--text); background: color-mix(in srgb, var(--text) 8%, var(--card-bg));
}
.badge {
  padding: 0.05rem 0.5rem; border-radius: 999px; font-size: 0.72rem; font-weight: 700; border: 1px solid var(--text-muted); color: var(--text-muted);
}
.badge.passed { color: var(--ok); border-color: var(--ok); background: color-mix(in srgb, var(--ok) 12%, var(--card-bg)); }
.badge.failed { color: var(--danger); border-color: var(--danger); background: color-mix(in srgb, var(--danger) 12%, var(--card-bg)); }
.board-empty, .dashboard-note { color: var(--text-muted); }
.console { margin: 0.4rem 0; border: 1px solid var(--card-border); border-radius: var(--radius); background: var(--card-bg); color: var(--text); }
.console summary { cursor: pointer; padding: 0.4rem 0.7rem; font-weight: 600; }
/* column-reverse anchors the scroll position to the bottom: the newest line stays in view as lines arrive. */
.console-scroll { display: flex; flex-direction: column-reverse; max-height: 18rem; overflow-y: auto; padding: 0 0.7rem 0.5rem 0.7rem; }
.console-line { display: flex; gap: 0.6rem; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.78rem; line-height: 1.5; }
.console-line .ts { color: var(--text-muted); }
.console-line .who { min-width: 3.2rem; font-weight: 700; color: var(--accent); }
.console-line .label { font-weight: 700; }
.console-line.task_end .what { font-weight: 700; }
.console-line.cost .what { color: var(--text-muted); }

/* Delivery dashboard. */
.dashboard { color: var(--text); }
.dashboard-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0.4rem 1rem; padding-right: 5rem; }  /* clear of the chat bubble's icons */
.dashboard-head h3 { margin: 0; }
.dashboard-verdict { padding: 0.15rem 0.7rem; border-radius: 999px; font-weight: 700; border: 1px solid var(--text-muted); }
.dashboard-verdict.passed { color: var(--ok); border-color: var(--ok); background: color-mix(in srgb, var(--ok) 12%, var(--card-bg)); }
.dashboard-verdict.failed { color: var(--danger); border-color: var(--danger); background: color-mix(in srgb, var(--danger) 12%, var(--card-bg)); }
.lane-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 0.6rem; margin: 0.6rem 0; }
.lane-card {
  --card-accent: var(--ok);
  padding: 0.6rem 0.8rem; border-radius: var(--radius); border: 1px solid var(--card-accent);
  background: color-mix(in srgb, var(--card-accent) 8%, var(--card-bg));
}
.lane-card.failed { --card-accent: var(--danger); }
.lane-card header { display: flex; justify-content: space-between; align-items: center; gap: 0.4rem; }
.lane-card p { margin: 0.3rem 0; }
.lane-label { color: var(--text-muted); font-size: 0.78rem; font-weight: 700; text-transform: uppercase; }
.lane-card ul { margin: 0.1rem 0 0.3rem 1.1rem; padding: 0; font-size: 0.85rem; }
.req-table { width: 100%; border-collapse: collapse; margin: 0.3rem 0 0.6rem 0; color: var(--text); }
.req-table th, .req-table td { padding: 0.25rem 0.5rem; text-align: left; border-bottom: 1px solid var(--card-border); }
.req-row.delivered td:last-child { color: var(--ok); font-weight: 600; }
.req-row.uncovered td:last-child, .req-row.untested td:last-child, .req-row.incomplete td:last-child { color: var(--danger); font-weight: 600; }
.stat-row { display: flex; flex-wrap: wrap; gap: 0.5rem; margin: 0.6rem 0; }
.stat { display: flex; flex-direction: column; padding: 0.4rem 0.8rem; border-radius: 10px; border: 1px solid var(--card-border); background: var(--card-bg); }
.stat-label { color: var(--text-muted); font-size: 0.75rem; }
.stat-value { font-weight: 700; }
.run-label { margin: 0.3rem 0 0 0; color: var(--text-muted); font-size: 0.8rem; font-weight: 700; }

@media (max-width: 640px) {
  /* One scrolling row: a wrapped stepper would fill the screen while it is sticky. */
  .stage-stepper { flex-wrap: nowrap; overflow-x: auto; width: 100%; padding-bottom: 0.15rem; }
  .stage-step { flex: 0 0 auto; min-width: 0; padding: 0.1rem 0.5rem; }
}
"""


def build_theme() -> gr.themes.ThemeClass:
    """Gradio's Soft theme with a system font stack (no web-font download); it follows light/dark on its own."""
    return gr.themes.Soft(
        primary_hue="indigo",
        secondary_hue="sky",
        neutral_hue="slate",
        font=("ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "sans-serif"),
        font_mono=("ui-monospace", "SFMono-Regular", "Menlo", "monospace"),
    )

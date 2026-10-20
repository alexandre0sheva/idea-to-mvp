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
  margin: 0.4rem 0 0.6rem 0; padding: 0.5rem 0.85rem;
  background: var(--card-bg); color: var(--text);
  border: 1px solid var(--card-border); border-radius: var(--radius);
}
/* The stepper: one row of equal-height steps, numbered by a CSS counter (a check mark once done, a cross when
   failed), joined by connector lines that stretch to fill the row. The running or failed step shows its time
   inline (a finished one in its tooltip), so a step never grows a second line. The badges move to a row of their
   own when the steps need the width; a row too narrow even then scrolls sideways instead of wrapping. */
.stage-stepper {
  display: flex; flex-wrap: nowrap; align-items: center; gap: 0.5rem; flex: 1 1 auto; min-width: min(100%, max-content);
  margin: 0 !important; padding: 0.15rem 0 !important; list-style: none; counter-reset: stage-step;
  overflow-x: auto; scrollbar-width: thin;
}
.stage-stepper > .stage-step {
  display: flex; align-items: center; gap: 0.4rem; flex: 1 1 auto; min-height: 1.6rem;
  margin: 0 !important; padding: 0; color: var(--text-muted);
  font-size: 0.8125rem; font-weight: 500; line-height: 1; white-space: nowrap;
  counter-increment: stage-step;
}
.stage-stepper > .stage-step:last-child { flex: 0 0 auto; }
.stage-stepper > .stage-step::marker { content: none; }
.stage-stepper > .stage-step::before {
  content: counter(stage-step); display: inline-flex; align-items: center; justify-content: center; flex: none;
  width: 1.35rem; height: 1.35rem; box-sizing: border-box; border-radius: 50%;
  border: 1px solid var(--card-border); background: var(--card-bg); color: var(--text-muted);
  font-size: 0.7rem; font-weight: 700; font-variant-numeric: tabular-nums;
}
.stage-stepper > .stage-step:not(:last-child)::after {
  content: ""; flex: 1 1 0.75rem; min-width: 0.75rem; height: 2px; border-radius: 1px; background: var(--card-border);
}
.stage-stepper > .stage-step:not(.active):not(.failed) .stage-time { display: none; }
.stage-step .stage-time {
  color: var(--text-muted); font-size: 0.72rem; font-weight: 500; line-height: 1; font-variant-numeric: tabular-nums;
}
.stage-stepper > .stage-step.done { color: var(--text); }
.stage-stepper > .stage-step.done::before { content: "✓"; border-color: var(--ok); background: var(--ok); color: var(--card-bg); }
.stage-stepper > .stage-step.done:not(:last-child)::after { background: var(--ok); }
.stage-stepper > .stage-step.active { color: var(--accent); font-weight: 700; }
.stage-stepper > .stage-step.active::before {
  border-color: var(--accent); background: var(--accent); color: var(--card-bg);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 25%, var(--card-bg));
}
.stage-stepper > .stage-step.failed { color: var(--danger); font-weight: 700; }
.stage-stepper > .stage-step.failed::before { content: "✕"; border-color: var(--danger); background: var(--danger); color: var(--card-bg); }
.usage-badges { display: flex; flex: none; gap: 0.35rem; margin-left: auto; }
.usage-badge {
  display: inline-flex; align-items: center; height: 1.6rem; padding: 0 0.6rem; box-sizing: border-box;
  border-radius: 999px; font-size: 0.75rem; font-weight: 600; line-height: 1; white-space: nowrap;
  font-variant-numeric: tabular-nums; color: var(--text); border: 1px solid var(--card-border);
  background: color-mix(in srgb, var(--accent) 8%, var(--card-bg));
}
.req-row td.req-acceptance.passed { color: var(--ok); font-weight: 600; }
.req-row td.req-acceptance.failed { color: var(--danger); font-weight: 600; }
.req-row td.req-acceptance.skipped, .req-row td.req-acceptance.no-test { color: var(--text-muted); font-weight: 400; }
.stat-row { display: flex; flex-wrap: wrap; gap: 0.5rem; margin: 0.6rem 0; }
.stat { display: flex; flex-direction: column; padding: 0.4rem 0.8rem; border-radius: 10px; border: 1px solid var(--card-border); background: var(--card-bg); }
.stat-label { color: var(--text-muted); font-size: 0.75rem; }
.stat-value { font-weight: 700; }
.run-label { margin: 0.3rem 0 0 0; color: var(--text-muted); font-size: 0.8rem; font-weight: 700; }

@media (max-width: 640px) {
  /* The stepper keeps its one row, swiped sideways without a scroll bar (a wrapped one would fill the screen
     while it is sticky), and leaves the times out; the badges may wrap. */
  .stage-stepper { scrollbar-width: none; }
  .stage-stepper::-webkit-scrollbar { display: none; }
  .stage-stepper > .stage-step .stage-time { display: none; }
  .usage-badges { flex: 1 1 100%; flex-wrap: wrap; min-width: 0; margin-left: 0; }
  .opening-row { grid-template-columns: 1fr; }
}
@media (prefers-reduced-motion: reduce) {
  .speaker-card.live-turn .speaker-body::after { animation: none; }
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

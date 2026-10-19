"""Small HTML components of the UI shell: the stage stepper, the usage badge, the model profile.

Pure functions from data to markup (no Gradio), styled by the CSS variables in `theme.py`.
"""

from __future__ import annotations

import html
from collections.abc import Mapping
from typing import Any

from idea_to_mvp.config import PROFILES, Settings
from idea_to_mvp.model_catalog import AgentRole, get_catalog
from idea_to_mvp.model_report import price_label
from idea_to_mvp.roles import ROLES, resolve_agent_model, resolve_role_model

AGENT_SESSION_LABELS: dict[AgentRole, str] = {
    "implementer": "task sessions",
    "verifier": "verification lanes",
    "escalation": "merge conflicts, later fixes",
}

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
# Graph state stages (and gate modes) that are not steps of their own.
STAGE_ALIASES: dict[str, str] = {
    "architecture": "arch_choice",
    "plan_gate": "plan_bundle",
    "implement_gate": "implementation",
    "report": "verification",
}

EXAMPLE_IDEAS: list[str] = [
    "A habit tracker for climbing gyms: members log their climbs, see progress per grade, and get a weekly recap.",
    "A tool for small bakeries to forecast tomorrow's demand from past sales and the weather, so they bake less waste.",
    "A browser extension that turns long research articles into a short brief with sources and open questions.",
    "A shared household chores board where roommates trade tasks, and the app keeps the split fair over time.",
]


def canonical_stage(stage: str) -> str:
    """The pipeline step a state stage belongs to ('' aliases resolved; unknown names returned unchanged)."""
    stage = (stage or "").strip()
    return STAGE_ALIASES.get(stage, stage)


def format_elapsed(seconds: float) -> str:
    """'8s', '1m 05s', '1h 02m': how long a stage took."""
    total = max(0, round(seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60:02d}s"
    return f"{total // 3600}h {(total % 3600) // 60:02d}m"


def stage_stepper(stage: str, statuses: Mapping[str, str], elapsed: Mapping[str, float]) -> str:
    """The pipeline as a row of steps: done / active / todo (or a status given in `statuses`, e.g. 'failed'),
    each with the time its stage took when known (`elapsed`, seconds, keyed by pipeline stage)."""
    key = canonical_stage(stage)
    keys = [k for k, _ in PIPELINE_STAGES]
    index = keys.index(key) if key in keys else 0
    items: list[str] = []
    for position, (step_key, label) in enumerate(PIPELINE_STAGES):
        if key == "done":
            status = "done" if position < len(PIPELINE_STAGES) - 1 else "active"
        elif position < index:
            status = "done"
        elif position == index:
            status = "active"
        else:
            status = "todo"
        status = statuses.get(step_key, status)
        current = " aria-current='step'" if status == "active" else ""
        took = elapsed.get(step_key, 0)
        time = f"<span class='stage-time'>{format_elapsed(took)}</span>" if took and round(took) > 0 else ""
        items.append(
            f"<li class='stage-step {html.escape(status, quote=True)}'{current}>"
            f"<span class='stage-label'>{html.escape(label)}</span>{time}</li>"
        )
    return "<ol class='stage-stepper'>" + "".join(items) + "</ol>"


def compact_count(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


def usage_badge(summary: Mapping[str, Any]) -> str:
    """Token total, planning spend, and agent spend of the run so far ('' before anything was spent).

    Planning calls (panel, summary, architecture, blueprint, ...) are priced from the model catalog; calls to a
    model with no price are called unpriced rather than left out silently."""
    if not summary.get("calls"):
        return ""
    tokens = int(summary.get("total_tokens") or 0)
    detail = f"{int(summary.get('input_tokens') or 0):,} in / {int(summary.get('output_tokens') or 0):,} out"
    badges = [f"<span class='usage-badge' title='{detail}'>{compact_count(tokens)} tokens</span>"]
    planning, unpriced = summary.get("planning_cost_usd"), int(summary.get("unpriced_calls") or 0)
    if planning is not None:
        extra = f" + {unpriced} unpriced" if unpriced else ""
        title = "Panel, research, summary, architecture, and blueprint calls at catalog prices" + (
            f"; {unpriced} call(s) to a model with no price are not in this figure" if unpriced else ""
        )
        badges.append(f"<span class='usage-badge cost' title='{title}'>${float(planning):.2f} planning{extra}</span>")
    elif unpriced:
        badges.append("<span class='usage-badge' title='These models have no price in the catalog'>planning unpriced</span>")
    if summary.get("agent_calls"):
        badges.append(
            f"<span class='usage-badge cost' title='Agent sessions: tasks, merge conflicts, verification, fixes'>"
            f"${float(summary.get('agent_cost_usd') or 0.0):.2f} agents</span>"
        )
    return "<span class='usage-badges'>" + "".join(badges) + "</span>"


def stage_header(stage: str, statuses: Mapping[str, str], elapsed: Mapping[str, float], usage: Mapping[str, Any]) -> str:
    """The sticky bar above the chat: the stepper and the usage badge."""
    return f"<div class='stage-header'>{stage_stepper(stage, statuses, elapsed)}{usage_badge(usage)}</div>"


def model_profile_markdown(settings: Settings) -> str:
    """Which provider and model each agent uses (read-only: change the profile or the models in `.env`)."""
    if settings.demo_mode:
        return "**Demo mode:** every agent answers from canned fixtures; no model is called."
    catalog = get_catalog()
    rows = [
        f"Model profile: **{settings.model_profile}**",
        "",
        "| Agent | Provider | Model | $ in / out per MTok |",
        "|---|---|---|---|",
    ]
    for key, spec in ROLES.items():
        provider, model = resolve_role_model(settings, key)
        pinned = {spec.provider_setting, spec.model_setting} & settings.model_fields_set
        note = " (overrides the profile)" if pinned else ""
        rows.append(f"| {key} | {provider} | `{model}`{note} | {price_label(catalog.find(model))} |")
    for kind, label in AGENT_SESSION_LABELS.items():
        model = resolve_agent_model(settings, kind)
        rows.append(f"| {kind} ({label}) | anthropic | `{model}` | {price_label(catalog.find(model))} |")
    typical = " · ".join(f"{name} ${catalog.estimate_run(name).total:.2f}" for name in PROFILES)
    return (
        "\n".join(rows)
        + f"\n\nEstimated cost of a typical run (three-task plan) per profile: {typical}."
        + "\n\nSet `MODEL_PROFILE` (fast, balanced, quality) and the per-role models in `.env` (see `.env.example`). "
        "`idea-to-mvp models` prints the full price list."
    )

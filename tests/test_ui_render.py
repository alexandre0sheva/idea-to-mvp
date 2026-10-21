"""The design system: stage stepper, usage badge, model profile, and a theme that reads in light and dark."""

import re

import gradio as gr
import pytest

from idea_to_mvp.config import Settings
from idea_to_mvp.ui.components import (
    EXAMPLE_IDEAS,
    PIPELINE_STAGES,
    format_elapsed,
    model_profile_markdown,
    stage_stepper,
    usage_badge,
)
from idea_to_mvp.ui.theme import CSS, build_theme

# ------------------------------------------------------------------- stepper

_STEP = re.compile(
    r"<li class='stage-step (\w+)'[^>]*>.*?<span class='stage-label'>(.*?)</span>", re.DOTALL
)


def steps(html: str) -> dict[str, str]:
    """label -> status class of every step in the rendered stepper."""
    return {label: status for status, label in _STEP.findall(html)}


def test_the_stepper_marks_done_active_and_todo_steps() -> None:
    found = steps(stage_stepper("strategy", {}, {}))
    assert list(found) == [label for _, label in PIPELINE_STAGES]
    assert (
        found["Panel"] == "done" and found["Strategy"] == "active" and found["Blueprint"] == "todo"
    )
    assert list(found.values()).count("active") == 1


def test_state_stages_map_onto_the_pipeline_steps() -> None:
    assert steps(stage_stepper("architecture", {}, {}))["Architecture"] == "active"
    assert steps(stage_stepper("plan_gate", {}, {}))["Blueprint"] == "active"
    assert steps(stage_stepper("implement_gate", {}, {}))["Implementation"] == "active"
    assert steps(stage_stepper("report", {}, {}))["Verification"] == "active"


def test_a_finished_run_has_every_step_done_and_the_last_one_current() -> None:
    found = steps(stage_stepper("done", {}, {}))
    assert "todo" not in found.values() and found["Done"] == "active"
    assert list(found.values()).count("done") == len(PIPELINE_STAGES) - 1


def test_an_unknown_stage_starts_at_the_beginning() -> None:
    assert steps(stage_stepper("nonsense", {}, {}))["Panel"] == "active"


def test_a_status_can_override_the_position_for_example_a_failed_verification() -> None:
    found = steps(stage_stepper("done", {"verification": "failed"}, {}))
    assert found["Verification"] == "failed" and found["Implementation"] == "done"


def test_the_current_step_is_announced_to_assistive_technology() -> None:
    html = stage_stepper("summary", {}, {})
    assert html.count("aria-current='step'") == 1
    assert re.search(r"stage-step active' aria-current='step'", html)


def test_elapsed_time_is_shown_only_for_stages_that_have_some() -> None:
    html = stage_stepper("strategy", {}, {"discussion": 8.2, "summary": 65.0, "answers": 0.0})
    assert html.count("class='stage-time'") == 2
    assert ">8s<" in html and ">1m 05s<" in html
    assert (
        "title='Panel: 8s'" in html and "title='Summary: 1m 05s'" in html
    )  # a finished step's time on hover
    assert "title='Answers" not in html


def test_stage_names_are_escaped() -> None:
    assert "<script" not in stage_stepper("<script>", {"<script>": "<script>"}, {"<script>": 5})


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0, "0s"),
        (-3, "0s"),
        (0.4, "0s"),
        (8.4, "8s"),
        (59.6, "1m 00s"),
        (65, "1m 05s"),
        (600, "10m 00s"),
        (3725, "1h 02m"),
    ],
)
def test_elapsed_time_formatting(seconds: float, text: str) -> None:
    assert format_elapsed(seconds) == text


# --------------------------------------------------------------------- badge


def summary(**overrides: object) -> dict:
    fields: dict = {
        "input_tokens": 9000, "output_tokens": 3300, "total_tokens": 12300, "calls": 7, "cost_usd": 1.454,
        "planning_cost_usd": 0.25, "agent_cost_usd": 1.204, "agent_calls": 3, "unpriced_calls": 0,
    }  # fmt: skip
    fields.update(overrides)
    return fields


def test_the_usage_badge_shows_planning_and_agent_spend_apart() -> None:
    html = usage_badge(summary())
    assert "12.3k tokens" in html and "$0.25 planning" in html and "$1.20 agents" in html
    assert "9,000 in" in html and "3,300 out" in html  # the exact numbers are in the tooltip


def test_the_usage_badge_omits_the_agent_badge_before_any_session_ran() -> None:
    html = usage_badge(summary(agent_cost_usd=0.0, agent_calls=0, total_tokens=1_500_000))
    assert "1.5M tokens" in html and "$0.25 planning" in html and "agents" not in html


def test_calls_without_a_price_are_called_unpriced() -> None:
    mixed = usage_badge(summary(unpriced_calls=2))
    assert (
        "$0.25 planning" in mixed and "unpriced" in mixed
    )  # the figure leaves some calls out, and says so
    none = usage_badge(
        summary(planning_cost_usd=None, unpriced_calls=4, agent_cost_usd=0.0, agent_calls=0)
    )
    assert "planning unpriced" in none and "$" not in none


def test_the_usage_badge_is_empty_before_anything_was_spent() -> None:
    assert usage_badge(summary(calls=0, total_tokens=0, cost_usd=None)) == ""
    assert usage_badge({}) == ""


def test_small_token_counts_are_shown_exactly() -> None:
    assert "850 tokens" in usage_badge(summary(total_tokens=850))


# ----------------------------------------------------------- model profile


def test_the_model_profile_lists_every_role_with_its_provider_and_model() -> None:
    text = model_profile_markdown(Settings(_env_file=None))
    assert "gpt-6.1-sol" in text and "claude-sonnet-5-5" in text and "gemini-3.8-flash" in text
    assert "implementer" in text.lower() and "Demo mode" not in text


def test_the_model_profile_names_the_preset_and_shows_the_models_it_resolves(monkeypatch) -> None:
    for key in ("PM_MODEL", "PM_PROVIDER", "ARCHITECT_MODEL", "ARCHITECT_PROVIDER"):
        monkeypatch.delenv(key, raising=False)
    text = model_profile_markdown(Settings(_env_file=None, model_profile="fast"))
    assert "fast" in text and "gpt-6-luna" in text and "claude-haiku-5-5" in text
    assert "gpt-6.1-sol" not in text


def test_a_role_pinned_in_the_environment_is_marked_as_overriding_the_profile() -> None:
    text = model_profile_markdown(
        Settings(_env_file=None, model_profile="fast", architect_model="my-model")
    )
    assert "my-model" in text and "overrides the profile" in text


def test_the_model_profile_says_when_demo_mode_makes_it_moot() -> None:
    assert "Demo mode" in model_profile_markdown(Settings(_env_file=None, demo_mode=True))


# ------------------------------------------------------------------ examples


def test_there_are_four_different_example_ideas_and_the_old_default_is_gone() -> None:
    assert len(EXAMPLE_IDEAS) == 4 and len(set(EXAMPLE_IDEAS)) == 4
    assert all(len(idea) > 40 for idea in EXAMPLE_IDEAS)
    assert not any("photographers" in idea for idea in EXAMPLE_IDEAS)


# --------------------------------------------------------------------- theme

_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
_THEME_SELECTOR = re.compile(r":root|data-theme|\.dark")


def rules(css: str) -> list[tuple[str, str]]:
    return [
        (selector.strip(), body)
        for selector, body in _RULE.findall(re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL))
    ]


def variables(scheme: str) -> dict[str, str]:
    """Colour variables in effect in a scheme: the :root values, overridden by the dark block."""
    found: dict[str, str] = {}
    for selector, body in rules(CSS):
        is_dark = "dark" in selector
        if _THEME_SELECTOR.search(selector) and (scheme == "dark" or not is_dark):
            found.update(dict(re.findall(r"(--[\w-]+):\s*([^;]+);", body)))
    return found


def test_the_theme_is_a_gradio_theme() -> None:
    assert isinstance(build_theme(), gr.themes.ThemeClass)


def test_no_colour_is_hard_coded_outside_the_variable_blocks() -> None:
    offenders = [
        selector
        for selector, body in rules(CSS)
        if not _THEME_SELECTOR.search(selector) and _HEX.search(body)
    ]
    assert offenders == []
    assert not re.search(
        r"rgba?\(",
        "".join(body for selector, body in rules(CSS) if not _THEME_SELECTOR.search(selector)),
    )


def test_every_variable_the_rules_use_is_defined() -> None:
    defined = set(variables("light"))
    used = set(re.findall(r"var\((--[\w-]+)", CSS))
    assert used <= defined, used - defined


def test_the_dark_scheme_restates_every_light_colour() -> None:
    light = {name for name, value in variables("light").items() if _HEX.fullmatch(value.strip())}
    dark = {name for name in variables("dark") if name in light}
    dark_block = {
        name
        for selector, body in rules(CSS)
        if "dark" in selector
        for name in re.findall(r"(--[\w-]+):", body)
    }
    assert light <= dark_block, light - dark_block
    assert dark == light


def _luminance(color: str) -> float:
    channels = [int(color.strip()[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _mix(accent: str, base: str, share: float) -> str:
    """`color-mix(in srgb, accent share%, base)` as a hex colour."""
    parts = [
        round(int(accent[i : i + 2], 16) * share + int(base[i : i + 2], 16) * (1 - share))
        for i in (1, 3, 5)
    ]
    return "#" + "".join(f"{p:02x}" for p in parts)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_text_is_readable_in_both_colour_schemes(scheme: str) -> None:
    v = {name: value.strip() for name, value in variables(scheme).items()}
    assert _contrast(v["--text"], v["--card-bg"]) >= 7
    assert _contrast(v["--text-muted"], v["--card-bg"]) >= 4.5
    assert _contrast(v["--text"], v["--page-bg"]) >= 7
    for accent in [
        name for name in v if name.startswith("--accent") or name in ("--ok", "--warn", "--danger")
    ]:
        card = _mix(v[accent], v["--card-bg"], 0.10)  # cards are tinted with their accent
        assert _contrast(v[accent], card) >= 4.5, f"{accent} on its tinted card in {scheme}"
        assert _contrast(v["--text"], card) >= 7, f"body text on the {accent} card in {scheme}"


def test_a_card_without_a_speaker_class_gets_a_readable_accent_not_the_border_colour() -> None:
    rule = CSS.split(".speaker-card {", 1)[1].split("}", 1)[0]
    assert "--card-accent: var(--accent-neutral)" in rule


def test_the_stepper_is_sticky_and_the_chat_gets_no_fixed_pixel_height() -> None:
    body = " ".join(body for selector, body in rules(CSS) if ".stage-tracker" in selector)
    assert "position: sticky" in body and "top: 0" in body
    container = " ".join(body for selector, body in rules(CSS) if ".gradio-container" in selector)
    assert "overflow: clip" in container  # Gradio's overflow: hidden would keep it from sticking
    assert "700px" not in CSS


def test_dark_mode_is_keyed_on_gradios_dark_class() -> None:
    assert any(".dark" in selector for selector, _ in rules(CSS))


# The classes whose rules a cleanup of the stylesheet once dropped (cards, gate forms, task board, console,
# dashboard): without them those parts render as unstyled text.
_STRUCTURAL_CLASSES = (
    "speaker-card", "speaker-head", "speaker-body", "opening-row", "arch-card", "arch-grid", "gate-table",
    "gate-banner", "cost-meter", "task-board", "board-col", "task-card", "badge", "console", "console-line",
    "dashboard", "dashboard-head", "dashboard-verdict", "lane-grid", "lane-card", "req-table", "diffstat",
)  # fmt: skip


@pytest.mark.parametrize("name", _STRUCTURAL_CLASSES)
def test_the_stylesheet_styles_the_structural_classes_of_the_html_renderers(name: str) -> None:
    assert re.search(rf"\.{re.escape(name)}\b[^{{]*{{", CSS), name

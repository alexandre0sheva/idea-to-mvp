"""Small helpers for driving SubmitService in tests."""

from __future__ import annotations

import re
from typing import Any

from idea_to_mvp.ui.gates import GATES, gate_layout, read_blueprint_file
from idea_to_mvp.ui.service import SubmitService
from idea_to_mvp.ui.view import (
    ARCH_CHOICE_B,
    IMPL_CHOICE_START,
    ITERATE_CHOICE_CHANGES,
    PLAN_CHOICE_GENERATE,
    mode_from_state,
)

# Positions in the tuple every SubmitService handler returns (see SubmitService._pack): the shared widgets,
# then one visibility update per gate panel, then one update per gate widget (in `gate_layout` order).
POS_STATUS, POS_CHAT, POS_INPUT, POS_ROUNDS, POS_BUTTON, POS_THREAD, POS_TRACKER, POS_SESSIONS, POS_EXAMPLES = range(9)
# The implementation view: tab selection, task board, live console, cost meter, and the artifacts panel.
POS_TABS, POS_BOARD, POS_CONSOLE, POS_METER, POS_PATH, POS_DELIVERY, POS_BLUEPRINT, POS_EXPLORER = range(9, 17)
POS_PANELS = POS_EXPLORER + 1
POS_FIELDS = POS_PANELS + len(GATES)


async def mode_of(service: SubmitService, thread: str) -> str:
    values, interrupt, next_nodes = await service._read(thread)
    return mode_from_state(values, interrupt, next_nodes=next_nodes)


def panel_visible(output: tuple[Any, ...], kind: str) -> Any:
    """Whether the form of a gate is shown in a handler's output."""
    return update_value(output[POS_PANELS + list(GATES).index(kind)], "visible")


def gate_field(output: tuple[Any, ...], kind: str, name: str) -> Any:
    """The update a handler's output carries for one gate widget."""
    return output[POS_FIELDS + gate_layout().index((kind, name))]


def legacy_inputs(kind: str, text: str, decision: str, payload: dict[str, Any]) -> dict[str, Any]:
    """The form inputs for what older tests express as free text plus a decision label."""
    if kind == "answers":
        lines = [re.sub(r"^\s*\d+[.)]\s*", "", line) for line in text.splitlines() if line.strip()]
        return {"action": "submit", **{f"answer_{i}": lines[i - 1] if i <= len(lines) else "" for i in range(1, 6)}}
    if kind == "arch_choice":
        return {"action": "submit", "option": "B" if decision == ARCH_CHOICE_B else "A", "notes": text}
    if kind == "plan_gate":
        return {"action": "generate" if decision == PLAN_CHOICE_GENERATE else "skip", "notes": text}
    if kind == "implement_gate":
        return {
            "action": "start" if decision == IMPL_CHOICE_START else "skip",
            "notes": text,
            "parallel": None,
            "file": "PRD.md",
            "editor": read_blueprint_file(payload.get("bundle_dir", ""), "PRD.md"),
        }
    if kind == "iterate_gate":
        return {"action": "iterate" if decision == ITERATE_CHOICE_CHANGES else "finish", "feedback": text}
    return {}


async def submit(
    service: SubmitService,
    text: str,
    decision: str,
    thread: str,
    rounds: int = 1,
    *,
    panel_mode: str | None = None,
    autopilot: bool = False,
    gate_inputs: dict[str, Any] | None = None,
    preferences: dict[str, Any] | None = None,
) -> list[tuple[Any, ...]]:
    """Submit like the UI does: an idea (or Continue) from the main box, or a gate decision as form inputs
    (`gate_inputs`, or derived from `text` + `decision` for the common cases)."""
    _values, interrupt, _next = await service._read(thread)
    kind = (interrupt or {}).get("kind")
    if gate_inputs is None and kind in GATES:
        gate_inputs = legacy_inputs(kind, text, decision, interrupt or {})
    return [
        output
        async for output in service.handle_submit(
            text, rounds, thread, panel_mode, autopilot, gate_inputs=gate_inputs, preferences=preferences
        )
    ]


def chat_text(output: tuple[Any, ...]) -> str:
    return " ".join(message["content"] for message in output[POS_CHAT])


def update_value(update: Any, key: str) -> Any:
    """Read a field from a `gr.update(...)` dict."""
    return update.get(key) if isinstance(update, dict) else getattr(update, key, None)

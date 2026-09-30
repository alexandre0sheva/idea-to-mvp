"""Live implementation events: what an agent session is doing, in a form the UI can show.

The executor turns Claude Agent SDK messages into `ImplEvent`s and publishes them with LangGraph's
custom stream writer; `SubmitService` subscribes to them and renders progress and the running cost.
SDK types are imported lazily so the rest of the app imports without the SDK.
"""

from __future__ import annotations

import time
from typing import Any, Literal, TypedDict

ImplEventKind = Literal["task_start", "tool", "text", "task_end", "cost"]
EVENT_KINDS: tuple[str, ...] = ("task_start", "tool", "text", "task_end", "cost")

_DETAIL_LIMIT = 160
# Tool inputs in order of how telling they are as a one-line description.
_DETAIL_KEYS = ("file_path", "notebook_path", "command", "pattern", "path", "description")


class ImplEvent(TypedDict):
    kind: ImplEventKind
    task_id: str | None
    label: str
    detail: str
    cost_usd: float | None
    ts: float


def make_event(
    kind: ImplEventKind,
    *,
    task_id: str | None = None,
    label: str = "",
    detail: str = "",
    cost_usd: float | None = None,
) -> ImplEvent:
    return {"kind": kind, "task_id": task_id, "label": label, "detail": detail, "cost_usd": cost_usd, "ts": time.time()}


def _shorten(text: str, limit: int = _DETAIL_LIMIT) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def _tool_detail(tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return ""
    for key in _DETAIL_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return _shorten(value)
    return ""


def events_from_sdk_message(message: Any, task_id: str | None) -> list[ImplEvent]:
    """Events for one SDK message: tool uses, visible text, and the final cost. Others give none."""
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock

    if isinstance(message, AssistantMessage):
        events: list[ImplEvent] = []
        for block in message.content:
            if isinstance(block, ToolUseBlock):
                events.append(make_event("tool", task_id=task_id, label=block.name, detail=_tool_detail(block.input)))
            elif isinstance(block, TextBlock) and block.text.strip():
                events.append(make_event("text", task_id=task_id, label="agent", detail=_shorten(block.text, 240)))
        return events
    if isinstance(message, ResultMessage) and message.total_cost_usd is not None:
        detail = f"{message.num_turns} turns"
        return [make_event("cost", task_id=task_id, label="session", detail=detail, cost_usd=float(message.total_cost_usd))]
    return []

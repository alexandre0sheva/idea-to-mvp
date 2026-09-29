"""Small helpers for driving SubmitService in tests."""

from __future__ import annotations

from typing import Any

from idea_to_mvp.ui.service import SubmitService
from idea_to_mvp.ui.view import mode_from_state

# Positions in the tuple every SubmitService handler returns (see SubmitService._pack).
POS_STATUS, POS_CHAT, POS_INPUT, POS_ROUNDS, POS_DECISION, POS_BUTTON, POS_THREAD, POS_TRACKER, POS_SESSIONS = range(9)


async def mode_of(service: SubmitService, thread: str) -> str:
    values, interrupt, next_nodes = await service._read(thread)
    return mode_from_state(values, interrupt, next_nodes=next_nodes)


async def submit(service: SubmitService, text: str, decision: str, thread: str, rounds: int = 1) -> list[tuple[Any, ...]]:
    return [output async for output in service.handle_submit(text, rounds, decision, thread)]


def chat_text(output: tuple[Any, ...]) -> str:
    return " ".join(message["content"] for message in output[POS_CHAT])


def update_value(update: Any, key: str) -> Any:
    """Read a field from a `gr.update(...)` dict."""
    return update.get(key) if isinstance(update, dict) else getattr(update, key, None)

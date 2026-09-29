"""Helpers shared by several nodes and by the UI service."""

from __future__ import annotations

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from idea_to_mvp.roles import TOKEN_TO_SPEAKER
from idea_to_mvp.text_utils import normalize_content


def display_speaker_name(name: str | None) -> str:
    if not name:
        return "Panelist"
    return TOKEN_TO_SPEAKER.get(name, name)


def history_markdown(messages: list[BaseMessage]) -> str:
    rows: list[str] = []
    turn_num = 0
    for msg in messages:
        if isinstance(msg, HumanMessage):
            rows.append(f"**[Human / anchor idea]**\n{normalize_content(msg.content)}")
        elif isinstance(msg, AIMessage):
            turn_num += 1
            name = display_speaker_name(msg.name)
            rows.append(f"**[{turn_num}. {name}]**\n{normalize_content(msg.content)}")
    return "\n\n".join(rows)

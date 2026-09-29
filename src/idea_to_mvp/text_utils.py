from __future__ import annotations

from typing import Any

from langchain_core.messages import BaseMessage


def normalize_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                maybe_text = item.get("text")
                if maybe_text:
                    parts.append(str(maybe_text))
        return "\n".join(parts).strip()
    return str(content)


def normalize_message_content(message: BaseMessage | None) -> str:
    if message is None:
        return ""
    return normalize_content(message.content)

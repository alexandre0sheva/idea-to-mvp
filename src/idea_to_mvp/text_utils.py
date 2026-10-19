from __future__ import annotations

from pathlib import Path
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


def clip(text: str, limit: int) -> str:
    """`text` on one line (runs of whitespace collapsed), cut to `limit` characters with an ellipsis."""
    one_line = " ".join((text or "").split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


def read_text(path: Path) -> str:
    """A text file's content, or '' when it cannot be read."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""

"""Optional tracing setup.

LangChain/LangGraph tracing (LangSmith) is configured entirely through `LANGSMITH_*` environment
variables. Our settings loader reads `.env` without exporting it, so only those variables are copied
into the process environment here — the provider API keys in `.env` are deliberately *not* exported
(agent subprocesses inherit the environment).
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values

TRACING_PREFIXES = ("LANGSMITH_", "LANGCHAIN_")


def apply_tracing_env(env_file: str | Path = ".env") -> list[str]:
    """Export `LANGSMITH_*`/`LANGCHAIN_*` from `env_file` unless already set; returns the names applied."""
    path = Path(env_file)
    if not path.is_file():
        return []
    applied: list[str] = []
    for key, value in dotenv_values(path).items():
        if value is None or not key.startswith(TRACING_PREFIXES) or key in os.environ:
            continue
        os.environ[key] = value
        applied.append(key)
    return sorted(applied)

"""LLM access layer.

Nodes call ``llm.get_runtime(...)`` / ``llm.invoke_text(...)`` / ``llm.invoke_structured(...)`` through this package namespace (not
``from ... import``) so tests can monkeypatch a single attribute for every node.
"""

from idea_to_mvp.llm.invoke import GroundedReply, extract_text, invoke_grounded, invoke_text
from idea_to_mvp.llm.runtime import (
    AgentRuntime,
    LlmRuntime,
    clear_runtime_caches,
    get_runtime,
    search_runtime,
)
from idea_to_mvp.llm.structured import StructuredOutputError, invoke_structured

__all__ = [
    "AgentRuntime",
    "GroundedReply",
    "LlmRuntime",
    "StructuredOutputError",
    "clear_runtime_caches",
    "extract_text",
    "get_runtime",
    "invoke_grounded",
    "invoke_structured",
    "invoke_text",
    "search_runtime",
]

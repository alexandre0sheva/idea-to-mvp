"""LangGraph orchestrator that takes a product idea to a built, tested MVP."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from idea_to_mvp.graph import build_graph
    from idea_to_mvp.ui.app import make_ui

__all__ = ["build_graph", "make_ui"]


def __getattr__(name: str) -> Any:
    # Lazy so `import idea_to_mvp.config` does not pull in gradio and the whole graph.
    if name == "build_graph":
        from idea_to_mvp.graph import build_graph

        return build_graph
    if name == "make_ui":
        from idea_to_mvp.ui.app import make_ui

        return make_ui
    raise AttributeError(f"module 'idea_to_mvp' has no attribute {name!r}")

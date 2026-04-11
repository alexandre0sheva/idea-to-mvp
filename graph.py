from __future__ import annotations

from functools import lru_cache

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

try:
    from .agents import (
        architect_node,
        discussion_node,
        route_after_discussion,
        route_from_start,
        summarizer_node,
    )
    from .config import get_settings
    from .state import IdeaDiscussionState
except ImportError:
    from agents import (
        architect_node,
        discussion_node,
        route_after_discussion,
        route_from_start,
        summarizer_node,
    )
    from config import get_settings
    from state import IdeaDiscussionState


@lru_cache(maxsize=1)
def build_graph(enable_checkpointer: bool | None = None) -> CompiledStateGraph:
    if enable_checkpointer is None:
        enable_checkpointer = get_settings().enable_checkpointer
    builder = StateGraph(IdeaDiscussionState)
    builder.add_node("discussion", discussion_node)
    builder.add_node("summarizer", summarizer_node)
    builder.add_node("architect", architect_node)

    builder.add_conditional_edges(
        START,
        route_from_start,
        {"discussion": "discussion", "architect": "architect"},
    )
    builder.add_conditional_edges(
        "discussion",
        route_after_discussion,
        {"discussion": "discussion", "summarizer": "summarizer"},
    )
    builder.add_edge("summarizer", END)
    builder.add_edge("architect", END)

    if enable_checkpointer:
        return builder.compile(checkpointer=MemorySaver())
    return builder.compile()


def clear_graph_cache() -> None:
    build_graph.cache_clear()

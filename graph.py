from __future__ import annotations

from functools import lru_cache

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

try:
    from .agents import (
        architect_node,
        collect_answers_node,
        discussion_node,
        plan_bundle_node,
        plan_gate_node,
        planner_offer_node,
        route_after_discussion,
        route_after_plan_gate,
        summarizer_node,
    )
    from .config import get_settings
    from .state import IdeaDiscussionState
except ImportError:
    from agents import (
        architect_node,
        collect_answers_node,
        discussion_node,
        plan_bundle_node,
        plan_gate_node,
        planner_offer_node,
        route_after_discussion,
        route_after_plan_gate,
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
    builder.add_node("collect_answers", collect_answers_node)
    builder.add_node("architect", architect_node)
    builder.add_node("planner_offer", planner_offer_node)
    builder.add_node("plan_gate", plan_gate_node)
    builder.add_node("plan_bundle", plan_bundle_node)

    builder.add_edge(START, "discussion")
    builder.add_conditional_edges(
        "discussion",
        route_after_discussion,
        {"discussion": "discussion", "summarizer": "summarizer"},
    )
    builder.add_edge("summarizer", "collect_answers")
    builder.add_edge("collect_answers", "architect")
    builder.add_edge("architect", "planner_offer")
    builder.add_edge("planner_offer", "plan_gate")
    builder.add_conditional_edges(
        "plan_gate",
        route_after_plan_gate,
        {"plan_bundle": "plan_bundle", "__end__": END},
    )
    builder.add_edge("plan_bundle", END)

    if enable_checkpointer:
        return builder.compile(checkpointer=MemorySaver())
    return builder.compile()


def clear_graph_cache() -> None:
    build_graph.cache_clear()

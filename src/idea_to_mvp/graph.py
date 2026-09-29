from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import aiosqlite
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from idea_to_mvp.config import Settings, get_settings
from idea_to_mvp.nodes.architecture import architect_node
from idea_to_mvp.nodes.blueprint import plan_bundle_node
from idea_to_mvp.nodes.discussion import discussion_node, route_after_discussion
from idea_to_mvp.nodes.gates import (
    arch_choice_node,
    collect_answers_node,
    implement_gate_node,
    plan_gate_node,
    route_after_implement_gate,
    route_after_plan_gate,
)
from idea_to_mvp.nodes.implement import implementer_node
from idea_to_mvp.nodes.report import delivery_report_node
from idea_to_mvp.nodes.strategy import strategy_node
from idea_to_mvp.nodes.summary import summarizer_node
from idea_to_mvp.nodes.verify import verifier_node
from idea_to_mvp.resilience import LLM_RETRY
from idea_to_mvp.state import IdeaDiscussionState


def build_graph(checkpointer: BaseCheckpointSaver | None = None) -> CompiledStateGraph:
    """Compile the pipeline graph. Gates use `interrupt()`, so real runs need a checkpointer;
    `None` is only useful for inspecting topology (diagram script)."""
    builder = StateGraph(IdeaDiscussionState)
    builder.add_node("discussion", discussion_node, retry_policy=LLM_RETRY)
    builder.add_node("summarizer", summarizer_node, retry_policy=LLM_RETRY)
    builder.add_node("collect_answers", collect_answers_node)
    builder.add_node("architect", architect_node, retry_policy=LLM_RETRY)
    builder.add_node("arch_choice", arch_choice_node)
    builder.add_node("strategy", strategy_node, retry_policy=LLM_RETRY)
    builder.add_node("plan_gate", plan_gate_node)
    builder.add_node("plan_bundle", plan_bundle_node, retry_policy=LLM_RETRY)
    builder.add_node("implement_gate", implement_gate_node)
    builder.add_node("implementer", implementer_node)
    builder.add_node("verifier", verifier_node)
    builder.add_node("delivery_report", delivery_report_node)

    builder.add_edge(START, "discussion")
    builder.add_conditional_edges(
        "discussion",
        route_after_discussion,
        {"discussion": "discussion", "summarizer": "summarizer"},
    )
    builder.add_edge("summarizer", "collect_answers")
    builder.add_edge("collect_answers", "architect")
    builder.add_edge("architect", "arch_choice")
    builder.add_edge("arch_choice", "strategy")
    builder.add_edge("strategy", "plan_gate")
    builder.add_conditional_edges(
        "plan_gate",
        route_after_plan_gate,
        {"plan_bundle": "plan_bundle", "__end__": END},
    )
    builder.add_edge("plan_bundle", "implement_gate")
    builder.add_conditional_edges(
        "implement_gate",
        route_after_implement_gate,
        {"implementer": "implementer", "__end__": END},
    )
    builder.add_edge("implementer", "verifier")
    builder.add_edge("verifier", "delivery_report")
    builder.add_edge("delivery_report", END)

    return builder.compile(checkpointer=checkpointer)


def run_config(thread_id: str) -> RunnableConfig:
    """Config for running a session's thread: names and tags the run so it is easy to find in traces."""
    return {
        "configurable": {"thread_id": thread_id},
        "run_name": "idea-to-mvp",
        "tags": ["idea-to-mvp", f"thread:{thread_id}"],
        "metadata": {"thread_id": thread_id},
    }


def pending_interrupt(snapshot: Any) -> dict[str, Any] | None:
    """Payload of the first interrupt a paused graph is waiting at, if any."""
    for task in getattr(snapshot, "tasks", None) or ():
        for interrupt in getattr(task, "interrupts", None) or ():
            value = getattr(interrupt, "value", None)
            return value if isinstance(value, dict) else {}
    return None


@asynccontextmanager
async def open_graph(settings: Settings | None = None) -> AsyncIterator[CompiledStateGraph]:
    """Compile the graph on the configured checkpointer and close it on exit."""
    settings = settings or get_settings()
    if settings.checkpointer == "memory":
        yield build_graph(MemorySaver())
        return
    path = settings.checkpoint_path
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = aiosqlite.connect(str(path))
    # aiosqlite's worker thread is non-daemon and would keep the process alive at exit if the
    # connection is never closed (e.g. Ctrl+C on the UI). Checkpoint writes are committed one by
    # one, so a daemon thread cannot lose data.
    worker = getattr(connection, "_thread", None)
    if worker is not None:
        worker.daemon = True
    async with connection:
        saver = AsyncSqliteSaver(connection)
        await saver.setup()
        yield build_graph(saver)


class GraphProvider:
    """Opens the graph lazily on first use, inside the event loop that will use it.

    Gradio builds the UI synchronously but serves requests from its own loop, and an aiosqlite
    connection must be created in a running loop, so `make_ui()` only holds a provider.
    """

    def __init__(self, settings: Settings | None = None, *, graph: CompiledStateGraph | None = None):
        self._settings = settings
        self._graph = graph
        self._stack: AsyncExitStack | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> CompiledStateGraph:
        if self._graph is None:
            async with self._lock:
                if self._graph is None:
                    stack = AsyncExitStack()
                    self._graph = await stack.enter_async_context(open_graph(self._settings))
                    self._stack = stack
        return self._graph

    async def aclose(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
            self._graph = None

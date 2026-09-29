"""Sessions survive a restart: state lives in SQLite, and a fresh graph/connection can continue it."""

from pathlib import Path

import pytest
from langgraph.types import Command

from idea_to_mvp import graph as graph_module
from idea_to_mvp.config import get_settings
from idea_to_mvp.graph import GraphProvider, open_graph, pending_interrupt
from idea_to_mvp.state import make_initial_state


def _config(thread: str) -> dict:
    return {"configurable": {"thread_id": thread}}


async def _drain(stream) -> list:
    return [event async for event in stream]


async def test_paused_gate_survives_reopening_the_sqlite_checkpointer(demo_env: Path) -> None:
    settings = get_settings()
    config = _config("persist-gate")

    async with open_graph(settings) as graph:
        await _drain(graph.astream(make_initial_state("A climbing app", 1), config, stream_mode="updates"))
        snapshot = await graph.aget_state(config)
        assert pending_interrupt(snapshot)["kind"] == "answers"

    assert settings.checkpoint_path.exists()

    # A brand-new graph + connection is what a restarted process sees.
    async with open_graph(settings) as graph:
        snapshot = await graph.aget_state(config)
        assert snapshot.next == ("collect_answers",)
        assert snapshot.values["user_idea"] == "A climbing app"
        assert len(snapshot.values["generated_questions"]) == 5

        events = await _drain(graph.astream(Command(resume="1. Solo climbers."), config, stream_mode="updates"))
        assert any("__interrupt__" in e for e in events)
        after = await graph.aget_state(config)
        assert after.values["user_answers"] == "1. Solo climbers."
        assert pending_interrupt(after)["kind"] == "arch_choice"


async def test_run_that_crashed_mid_step_continues_from_its_checkpoint(
    demo_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    config = _config("persist-crash")
    real_strategy = graph_module.strategy_node

    def exploding_strategy(state):
        raise RuntimeError("process died")

    async with open_graph(settings) as graph:
        await _drain(graph.astream(make_initial_state("A climbing app", 1), config, stream_mode="updates"))
        await _drain(graph.astream(Command(resume="answers"), config, stream_mode="updates"))
    monkeypatch.setattr(graph_module, "strategy_node", exploding_strategy)
    async with open_graph(settings) as graph:
        with pytest.raises(RuntimeError, match="process died"):
            await _drain(graph.astream(Command(resume={"option": "A", "notes": ""}), config, stream_mode="updates"))
        snapshot = await graph.aget_state(config)
        assert snapshot.next == ("strategy",) and pending_interrupt(snapshot) is None
        assert snapshot.values["arch_choice"]["option"] == "A"  # the gate decision was checkpointed

    monkeypatch.setattr(graph_module, "strategy_node", real_strategy)
    async with open_graph(settings) as graph:
        events = await _drain(graph.astream(None, config, stream_mode="updates"))  # continue, no new input
        assert any("strategy" in e for e in events)
        assert pending_interrupt(await graph.aget_state(config))["kind"] == "plan_gate"


async def test_memory_checkpointer_needs_no_files(demo_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHECKPOINTER", "memory")
    from idea_to_mvp.config import clear_settings_cache

    clear_settings_cache()
    settings = get_settings()
    async with open_graph(settings) as graph:
        await _drain(graph.astream(make_initial_state("An idea", 1), _config("mem"), stream_mode="updates"))
        assert pending_interrupt(await graph.aget_state(_config("mem")))["kind"] == "answers"
    assert not settings.checkpoint_path.exists()


async def test_provider_opens_lazily_and_closes(demo_env: Path) -> None:
    settings = get_settings()
    provider = GraphProvider(settings)
    assert not settings.checkpoint_path.exists()
    graph = await provider.get()
    assert await provider.get() is graph  # cached
    assert settings.checkpoint_path.exists()
    await provider.aclose()
    assert await provider.get() is not graph  # reopens after close
    await provider.aclose()

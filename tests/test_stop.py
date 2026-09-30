"""Stop: cancelling a run leaves a consistent checkpoint, and the session offers to continue from it."""

import asyncio
import contextlib
from typing import Any

from service_helpers import POS_BUTTON, POS_STATUS, chat_text, mode_of, submit, update_value

from idea_to_mvp import graph as graph_module
from idea_to_mvp.ui.view import MODE_ANSWERS, MODE_ARCH_CHOICE, MODE_INTERRUPTED

IDEA = "A habit tracker for climbing gyms"


class SlowArchitect:
    """Replaces the architect node with one that can be held mid-run (and released afterwards)."""

    def __init__(self, monkeypatch: Any) -> None:
        self.real = graph_module.architect_node
        self.slow = True
        self.started = asyncio.Event()
        monkeypatch.setattr(graph_module, "architect_node", self.node)

    async def node(self, state: Any) -> Any:
        if self.slow:
            self.started.set()
            await asyncio.sleep(60)  # cancelled long before this ends
        return self.real(state)


async def run_and_cancel(service: Any, hold: SlowArchitect, thread: str) -> None:
    """Start the answers submit, wait until the architect is running, then cancel the run as Gradio would."""

    async def consume() -> None:
        answers = {"action": "submit", "answer_1": "Solo climbers.", "answer_2": "", "answer_3": "", "answer_4": "", "answer_5": ""}
        async for _ in service.handle_submit("", 1, thread, gate_inputs=answers):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(hold.started.wait(), timeout=20)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_stopping_a_run_keeps_its_progress_and_offers_to_continue(demo_service: Any, monkeypatch: Any) -> None:
    service, graphs = demo_service
    hold = SlowArchitect(monkeypatch)
    thread = "stop-1"
    await submit(service, IDEA, "", thread)
    assert await mode_of(service, thread) == MODE_ANSWERS

    await run_and_cancel(service, hold, thread)
    view = await service.stop_run(thread)

    assert await mode_of(service, thread) == MODE_INTERRUPTED
    assert "Stopped" in str(update_value(view[POS_STATUS], "value")) and "Continue" in str(update_value(view[POS_STATUS], "value"))
    assert update_value(view[POS_BUTTON], "value") == "Continue"
    graph = await graphs.get()
    snapshot = await graph.aget_state({"configurable": {"thread_id": thread}})
    assert snapshot.next == ("architect",)  # the step that was running is the one to redo
    assert snapshot.values["user_answers"].startswith("1. Solo climbers")  # what was decided is kept
    assert snapshot.values["architecture"] == ""  # and nothing half-done was written


async def test_a_stopped_run_continues_to_the_next_gate(demo_service: Any, monkeypatch: Any) -> None:
    service, graphs = demo_service
    hold = SlowArchitect(monkeypatch)
    thread = "stop-2"
    await submit(service, IDEA, "", thread)
    await run_and_cancel(service, hold, thread)

    hold.slow = False
    outputs = await submit(service, "", "", thread)  # the Continue button
    assert await mode_of(service, thread) == MODE_ARCH_CHOICE
    assert "Architecture" in chat_text(outputs[-1]) or "Option A" in chat_text(outputs[-1])
    graph = await graphs.get()
    values = (await graph.aget_state({"configurable": {"thread_id": thread}})).values
    assert values["architecture"] and values["architecture_proposal"]


async def test_stopping_while_the_run_waits_at_a_gate_changes_nothing(demo_service: Any) -> None:
    service, _ = demo_service
    thread = "stop-3"
    await submit(service, IDEA, "", thread)
    view = await service.stop_run(thread)
    assert await mode_of(service, thread) == MODE_ANSWERS
    assert "Stopped" not in str(update_value(view[POS_STATUS], "value"))


async def test_autopilot_switched_on_while_stopped_applies_when_the_run_continues(demo_service: Any, monkeypatch: Any) -> None:
    from idea_to_mvp.ui.view import MODE_IMPL_GATE

    service, graphs = demo_service
    hold = SlowArchitect(monkeypatch)
    thread = "stop-4"
    await submit(service, IDEA, "", thread)
    await run_and_cancel(service, hold, thread)

    hold.slow = False
    await submit(service, "", "", thread, autopilot=True)  # Continue, with the switch now on
    assert await mode_of(service, thread) == MODE_IMPL_GATE
    graph = await graphs.get()
    assert (await graph.aget_state({"configurable": {"thread_id": thread}})).values["autopilot"] is True

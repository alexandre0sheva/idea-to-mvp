"""The live implementation view through the real service (demo mode): board, console, meter, tabs, artifacts."""

import asyncio
import contextlib
import re
from pathlib import Path
from typing import Any

import pytest
from service_helpers import (
    POS_BLUEPRINT,
    POS_BOARD,
    POS_CONSOLE,
    POS_DELIVERY,
    POS_EXPLORER,
    POS_METER,
    POS_PATH,
    POS_STATUS,
    POS_TABS,
    chat_text,
    mode_of,
    submit,
    update_value,
)

from idea_to_mvp.config import clear_settings_cache
from idea_to_mvp.implementation import executor
from idea_to_mvp.ui.view import IMPL_CHOICE_START, MODE_IMPL_GATE, MODE_ITERATE_GATE

IDEA = "A habit tracker for climbing gyms"


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    clear_settings_cache()


def board_columns(output: tuple[Any, ...]) -> dict[str, list[str]]:
    html = str(update_value(output[POS_BOARD], "value") or "")
    found: dict[str, list[str]] = {}
    for name, body in re.findall(r"<section class='board-col' data-col='(\w+)'>(.*?)</section>", html, flags=re.DOTALL):
        found[name] = re.findall(r"data-task='([^']+)'", body)
    return found


async def to_implement_gate(service: Any, thread: str) -> None:
    await submit(service, IDEA, "", thread)
    await submit(service, "1. Solo climbers.", "", thread)
    await submit(service, "", "", thread)
    await submit(service, "", "Generate the execution pack", thread)
    assert await mode_of(service, thread) == MODE_IMPL_GATE


async def test_tasks_move_across_the_board_while_the_console_fills(demo_service: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    service, _ = demo_service
    thread = "iv-board"
    real = executor._run_demo_session

    async def unhurried(workspace: Path, task: Any, emit: Any) -> Any:
        await asyncio.sleep(0.5)  # long enough that a slow runner renders "running" before the task finishes
        return await real(workspace, task, emit)

    monkeypatch.setattr(executor, "_run_demo_session", unhurried)
    await to_implement_gate(service, thread)
    outputs = await submit(service, "", IMPL_CHOICE_START, thread)

    seen = [board_columns(output) for output in outputs]
    state_of = lambda task, cols: next((name for name, ids in cols.items() if task in ids), None)  # noqa: E731
    path = [state_of("T01", cols) for cols in seen if state_of("T01", cols)]
    assert "running" in path and path[-1] == "done"  # it was on the board as running, and ended as done
    assert path.index("running") < path.index("done") and "pending" not in path[path.index("done") :]
    assert "running" not in path[path.index("done") :]  # a finished task never flickers back to running
    final = seen[-1]
    assert final["done"] == ["T01", "T02", "T03"] and final["running"] == [] and final["failed"] == []

    console = str(update_value(outputs[-1][POS_CONSOLE], "value"))
    assert "console-line" in console and "T01" in console and "started: Project setup" in console
    assert any("task_end" in str(update_value(o[POS_CONSOLE], "value")) for o in outputs)  # lines arrived while it ran


async def test_the_cost_meter_shows_the_run_budget(demo_service: Any) -> None:
    service, _ = demo_service
    thread = "iv-meter"
    await to_implement_gate(service, thread)
    outputs = await submit(service, "", IMPL_CHOICE_START, thread)
    assert "of $25.00" in str(update_value(outputs[-1][POS_METER], "value"))


async def test_the_view_switches_to_the_implementation_tab_when_it_starts_and_back_when_it_ends(demo_service: Any) -> None:
    service, _ = demo_service
    thread = "iv-tabs"
    await to_implement_gate(service, thread)
    outputs = await submit(service, "", IMPL_CHOICE_START, thread)
    selections = [update_value(o[POS_TABS], "selected") for o in outputs]
    assert [s for s in selections if s] == ["implementation", "conversation"]  # each switch once, not on every render
    assert selections.index("implementation") < selections.index("conversation")  # and the last render sends nothing


async def test_the_delivery_dashboard_replaces_the_markdown_report(demo_service: Any) -> None:
    service, _ = demo_service
    thread = "iv-dashboard"
    await to_implement_gate(service, thread)
    outputs = await submit(service, "", IMPL_CHOICE_START, thread)
    assert await mode_of(service, thread) == MODE_ITERATE_GATE
    chat = chat_text(outputs[-1])
    assert "Delivery report — v0.1" in chat and "lane-card" in chat and "How to run" in chat and "req-table" in chat
    assert "### Files" not in chat  # not the markdown report
    assert chat.count("Delivery report") == 1


async def test_the_artifacts_panel_points_at_the_workspace_and_offers_both_zips(demo_service: Any, demo_env: Path) -> None:
    service, graphs = demo_service
    thread = "iv-artifacts"
    await to_implement_gate(service, thread)
    outputs = await submit(service, "", IMPL_CHOICE_START, thread)
    graph = await graphs.get()
    values = (await graph.aget_state({"configurable": {"thread_id": thread}})).values


    def latest(pos: int, key: str) -> Any:
        """The last value the run sent for a widget (the panel is only refreshed when its content changes)."""
        return next((update_value(o[pos], key) for o in reversed(outputs) if update_value(o[pos], key) is not None), None)

    assert latest(POS_PATH, "value") == values["workspace_dir"]
    assert latest(POS_DELIVERY, "value") == values["delivery_zip"]
    assert [update_value(o[POS_DELIVERY], "visible") for o in outputs if update_value(o[POS_DELIVERY], "visible") is not None][-1] is True
    blueprint = latest(POS_BLUEPRINT, "value")
    assert blueprint.endswith("-blueprint.zip") and Path(blueprint).exists() and Path(blueprint).is_relative_to(demo_env / "deliveries")
    # the explorer is rebuilt from this state, when the workspace appears and when the delivery lands only
    workspaces = [o[POS_EXPLORER] for o in outputs if isinstance(o[POS_EXPLORER], str) and o[POS_EXPLORER]]
    assert workspaces and set(workspaces) == {values["workspace_dir"]} and len(workspaces) <= 3


async def test_a_reloaded_page_gets_the_panel_again(demo_service: Any) -> None:
    service, _ = demo_service
    thread = "iv-reload"
    await to_implement_gate(service, thread)
    await submit(service, "", IMPL_CHOICE_START, thread)
    reloaded = await service.initial_view(thread)
    assert isinstance(reloaded[POS_EXPLORER], str) and reloaded[POS_EXPLORER] and "Delivery report" in chat_text(reloaded)
    assert "done" in str(update_value(reloaded[POS_BOARD], "value"))


async def test_the_file_viewer_shows_workspace_files_and_nothing_else(demo_service: Any) -> None:
    service, graphs = demo_service
    thread = "iv-viewer"
    await to_implement_gate(service, thread)
    await submit(service, "", IMPL_CHOICE_START, thread)
    graph = await graphs.get()
    workspace = (await graph.aget_state({"configurable": {"thread_id": thread}})).values["workspace_dir"]
    shown = await service.view_file(thread, "README.md")
    assert "Demo MVP" in update_value(shown, "value")
    outside = await service.view_file(thread, "../../sessions.db")
    assert update_value(outside, "value") == "" and Path(workspace).is_dir()
    assert update_value(await service.view_file("no-such-thread", "README.md"), "value") == ""


async def test_a_stopped_implementation_leaves_no_task_marked_as_running(demo_service: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMPLEMENTER_MAX_PARALLEL", "1")
    clear_settings_cache()
    service, _ = demo_service
    thread = "iv-stop"
    started = asyncio.Event()
    real = executor._run_demo_session

    async def slow(workspace: Path, task: Any, emit: Any) -> Any:
        if task.id == "T02":
            started.set()
            await asyncio.sleep(60)
        return await real(workspace, task, emit)

    monkeypatch.setattr(executor, "_run_demo_session", slow)
    await to_implement_gate(service, thread)
    seen: list[tuple[Any, ...]] = []

    async def consume() -> None:
        async for output in service.handle_submit("", 1, thread, gate_inputs={"action": "start", "notes": "", "parallel": None, "file": "PRD.md", "editor": Path((await service.gate_payload(thread))["bundle_dir"], "PRD.md").read_text()}):
            seen.append(output)

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), timeout=30)
    await asyncio.sleep(0.1)
    assert any(board_columns(o).get("running") == ["T02"] for o in seen)  # it was running
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    stopped = await service.stop_run(thread)
    cols = board_columns(stopped)
    assert cols["running"] == [] and cols["done"] == ["T01"] and "T02" in cols["pending"]
    assert "Stopped" in str(update_value(stopped[POS_STATUS], "value"))

"""The MCP server: the five tools drive a whole demo run, the spend gates need `confirm_spend`, and the CLI starts it."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.server.mcpserver.exceptions import ToolError

from idea_to_mvp import cli, mcp_server
from idea_to_mvp.config import get_settings
from idea_to_mvp.mcp_server import GateAnswer, build_resume, create_server

IDEA = "a habit tracker"
QUESTIONS = [
    {"question": f"Q{n}?", "why_it_matters": "it matters", "suggested_answer": f"suggestion {n}"} for n in range(1, 6)
]


async def call(client: Client, name: str, **arguments: Any) -> dict[str, Any]:
    result = await client.call_tool(name, arguments)
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return result.structured_content


async def call_error(client: Client, name: str, **arguments: Any) -> str:
    """The message of a tool call that failed."""
    try:
        result = await client.call_tool(name, arguments)
    except Exception as error:
        return str(error)
    assert result.is_error, result.structured_content
    return " ".join(getattr(block, "text", "") for block in result.content)


async def settled(client: Client, thread: str, until: Callable[[dict[str, Any]], bool] = lambda s: not s["running"]) -> dict[str, Any]:
    """`get_status` until the run is idle (or `until` says so)."""
    for _ in range(600):
        status = await call(client, "get_status", thread_id=thread)
        if until(status):
            return status
        await asyncio.sleep(0.05)
    raise AssertionError(f"still not settled: {status}")


@pytest.fixture()
def server(demo_env: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(demo_env)  # never pick up the developer's real .env
    return create_server(get_settings())


# ------------------------------------------------------------------ the tools


async def test_the_server_offers_exactly_the_five_tools(server) -> None:
    async with Client(server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert set(tools) == {"start_run", "get_status", "answer_gate", "list_sessions", "get_delivery"}
    assert all(tool.description for tool in tools.values())
    assert "confirm_spend" in tools["answer_gate"].input_schema["properties"]
    read_only = {name for name, tool in tools.items() if tool.annotations and tool.annotations.read_only_hint}
    assert read_only == {"get_status", "list_sessions", "get_delivery"}


async def test_a_whole_demo_run_through_the_tools(server, demo_env: Path) -> None:
    async with Client(server) as client:
        started = await call(client, "start_run", idea=IDEA, autopilot=True)
        thread = started["thread_id"]
        assert started["state"] == "running"

        status = await settled(client, thread)
        assert status["state"] == "waiting" and status["gate"]["kind"] == "implement_gate"
        estimate = status["gate"]["payload"]["cost_estimate"]
        assert estimate["tasks"] >= 1 and status["cost"]["estimate"] == estimate
        assert status["cost"]["total_tokens"] > 0 and status["workspace"] == ""
        assert not (demo_env / "projects").exists()  # no agent session yet

        # the implement gate spends money: no confirmation, no agents
        refused = await call_error(client, "answer_gate", thread_id=thread, gate="implement_gate", implement=True)
        assert "confirm_spend" in refused
        assert (await call(client, "get_status", thread_id=thread))["gate"]["kind"] == "implement_gate"
        assert not (demo_env / "projects").exists()

        accepted = await call(
            client, "answer_gate", thread_id=thread, gate="implement_gate", implement=True, confirm_spend=True, parallel=1
        )
        assert accepted["state"] == "running"
        status = await settled(client, thread)
        assert status["state"] == "delivered" and status["verification"] == "passed"
        assert status["gate"]["kind"] == "iterate_gate" and "report" not in status["gate"]["payload"]
        assert any("start T01" in line for line in status["progress"])

        delivery = await call(client, "get_delivery", thread_id=thread)
        assert delivery["verification"] == "passed" and delivery["iteration"] == 1
        assert delivery["report"].strip() and Path(delivery["delivery_zip"]).is_file()
        assert Path(delivery["workspace"]).is_relative_to(demo_env / "projects")

        rows = (await call(client, "list_sessions"))["sessions"]
        assert rows[0]["thread_id"] == thread and rows[0]["waiting_at"] == "iterate_gate" and rows[0]["running"] is False


async def test_every_gate_is_answered_with_its_own_arguments(server) -> None:
    async with Client(server) as client:
        thread = (await call(client, "start_run", idea=IDEA))["thread_id"]
        status = await settled(client, thread)
        assert status["gate"]["kind"] == "answers" and len(status["gate"]["payload"]["question_items"]) == 5
        assert "answers" in status["gate"]["answer_with"]

        # the gate must be named, so a stale answer cannot land on the wrong one
        assert "waits at the answers gate" in await call_error(
            client, "answer_gate", thread_id=thread, gate="arch_choice", option="A"
        )

        await call(client, "answer_gate", thread_id=thread, gate="answers", answers="Solo climbers\nLogging only")
        status = await settled(client, thread)
        assert status["gate"]["kind"] == "arch_choice" and status["gate"]["payload"]["proposal"]

        await call(client, "answer_gate", thread_id=thread, gate="arch_choice", option="B")
        status = await settled(client, thread)
        assert status["gate"]["kind"] == "plan_gate"

        await call(client, "answer_gate", thread_id=thread, gate="plan_gate", generate=True)
        status = await settled(client, thread)
        assert status["gate"]["kind"] == "implement_gate"

        # declining to build spends nothing and needs no confirmation: the run ends
        await call(client, "answer_gate", thread_id=thread, gate="implement_gate", implement=False)
        status = await settled(client, thread)
        assert status["state"] == "ended" and status["gate"] is None


async def test_a_new_version_needs_feedback_and_confirm_spend(server) -> None:
    async with Client(server) as client:
        thread = (await call(client, "start_run", idea=IDEA, autopilot=True))["thread_id"]
        await settled(client, thread)
        await call(client, "answer_gate", thread_id=thread, gate="implement_gate", implement=True, confirm_spend=True)
        assert (await settled(client, thread))["state"] == "delivered"

        assert "confirm_spend" in await call_error(
            client, "answer_gate", thread_id=thread, gate="iterate_gate", feedback="add a streak counter"
        )
        assert "feedback" in await call_error(client, "answer_gate", thread_id=thread, gate="iterate_gate", confirm_spend=True)

        await call(
            client, "answer_gate", thread_id=thread, gate="iterate_gate", feedback="add a streak counter", confirm_spend=True
        )
        status = await settled(client, thread)
        assert status["state"] == "delivered" and status["iteration"] == 2
        assert (await call(client, "get_delivery", thread_id=thread))["iteration"] == 2


async def test_unknown_sessions_and_undelivered_runs_are_tool_errors(server) -> None:
    async with Client(server) as client:
        assert "no saved session" in await call_error(client, "get_status", thread_id="nope")
        assert "no saved session" in await call_error(client, "get_delivery", thread_id="nope")
        assert "no saved session" in await call_error(client, "answer_gate", thread_id="nope", gate="answers", answers="x")
        assert "idea" in await call_error(client, "start_run", idea="   ")
        assert (await call(client, "list_sessions"))["sessions"] == []

        thread = (await call(client, "start_run", idea=IDEA))["thread_id"]
        await settled(client, thread)
        assert "not delivered" in await call_error(client, "get_delivery", thread_id=thread)


async def test_a_session_that_is_running_takes_no_second_answer(
    server, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = asyncio.Event()
    seen: list[str] = []

    async def slow_run(graph, registry, idea, options, *, settings, progress, thread_id, preferences=None):
        seen.append(thread_id)
        registry.record(thread_id, {}, None, title=idea)
        progress("fake step")
        await release.wait()
        return mcp_server.summarize(thread_id, await mcp_server.read_thread(graph, thread_id))

    monkeypatch.setattr(mcp_server, "run_idea", slow_run)
    async with Client(server) as client:
        thread = (await call(client, "start_run", idea=IDEA))["thread_id"]
        status = await settled(client, thread, lambda s: s["progress"] != [])
        assert status["state"] == "running" and status["running"] and status["progress"] == ["fake step"]
        assert "still running" in await call_error(client, "answer_gate", thread_id=thread, gate="answers", answers="x")
        assert (await call(client, "list_sessions"))["sessions"][0]["running"] is True
        release.set()
        await asyncio.sleep(0.1)
        assert (await call(client, "list_sessions"))["sessions"][0]["running"] is False
    assert seen == [thread]


async def test_a_pipeline_error_is_reported_in_the_status(server, monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken_run(*_args: object, **_kwargs: object):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(mcp_server, "run_idea", broken_run)
    async with Client(server) as client:
        thread = (await call(client, "start_run", idea=IDEA))["thread_id"]
        status = await settled(client, thread)
    assert status["state"] == "error" and "provider exploded" in status["error"]


# ------------------------------------------------------------------ answers to resume values


def test_an_answers_gate_takes_text_or_the_suggestions() -> None:
    payload = {"kind": "answers", "question_items": QUESTIONS}
    assert build_resume(payload, GateAnswer(answers="Solo climbers\n\n3. Logging only")).resume == (
        "1. Solo climbers\n3. Logging only"
    )
    suggested = build_resume(payload, GateAnswer(use_suggested_answers=True)).resume
    assert suggested.splitlines()[0] == "1. suggestion 1" and len(suggested.splitlines()) == 5
    for empty in (GateAnswer(), GateAnswer(answers="\n \n")):
        with pytest.raises(ToolError, match="answers"):
            build_resume(payload, empty)
    with pytest.raises(ToolError, match="no suggested"):
        build_resume({"kind": "answers", "question_items": []}, GateAnswer(use_suggested_answers=True))


def test_an_architecture_needs_a_letter_and_the_pack_an_explicit_yes() -> None:
    assert build_resume({"kind": "arch_choice"}, GateAnswer(option="b", notes="scale")).resume == {
        "option": "B",
        "notes": "scale",
    }
    with pytest.raises(ToolError, match="option"):
        build_resume({"kind": "arch_choice"}, GateAnswer())
    with pytest.raises(ToolError, match="option"):
        build_resume({"kind": "arch_choice"}, GateAnswer(option="C"))
    assert build_resume({"kind": "plan_gate"}, GateAnswer(generate=True)).resume == {"generate": True, "notes": ""}
    assert build_resume({"kind": "plan_gate"}, GateAnswer(generate=False)).resume["generate"] is False
    with pytest.raises(ToolError, match="generate"):
        build_resume({"kind": "plan_gate"}, GateAnswer())


def test_only_the_gates_that_start_agent_sessions_need_confirm_spend() -> None:
    gate = {"kind": "implement_gate"}
    with pytest.raises(ToolError, match="confirm_spend"):
        build_resume(gate, GateAnswer(implement=True))
    built = build_resume(gate, GateAnswer(implement=True, confirm_spend=True, parallel=2, harness_tuning=True))
    assert built.resume == {"implement": True, "notes": "", "parallel": 2, "harness_tuning": True}
    # the gate's own default for harness tuning stands unless the caller sets it
    assert "harness_tuning" not in build_resume(gate, GateAnswer(implement=True, confirm_spend=True)).resume
    assert build_resume(gate, GateAnswer(implement=False)).resume["implement"] is False
    with pytest.raises(ToolError, match="implement"):
        build_resume(gate, GateAnswer())

    iterate = {"kind": "iterate_gate"}
    with pytest.raises(ToolError, match="confirm_spend"):
        build_resume(iterate, GateAnswer(feedback="dark mode"))
    assert build_resume(iterate, GateAnswer(feedback=" dark mode ", confirm_spend=True)).resume == {
        "iterate": True,
        "feedback": "dark mode",
    }
    with pytest.raises(ToolError, match="feedback"):
        build_resume(iterate, GateAnswer(confirm_spend=True))
    with pytest.raises(ToolError, match="unknown gate"):
        build_resume({"kind": "mystery"}, GateAnswer())


# ------------------------------------------------------------------ the command


def test_the_mcp_command_starts_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[bool] = []
    monkeypatch.setattr(mcp_server, "serve", lambda: started.append(True) or 0)
    assert cli.main(["mcp"]) == 0 and started == [True]


def test_the_mcp_command_says_how_to_install_a_missing_package(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(sys.modules, "idea_to_mvp.mcp_server", None)  # `import` raises ImportError
    assert cli.main(["mcp"]) == 1
    assert "idea-to-mvp[mcp]" in capsys.readouterr().err


async def test_the_server_speaks_mcp_over_stdio(demo_env: Path) -> None:
    """The real command in a subprocess: stdout carries only protocol, so a stray print cannot break a client."""
    env = {"DEMO_MODE": "true", "OUTPUT_DIR": str(demo_env), "CHECKPOINTER": "sqlite"}
    params = StdioServerParameters(command=sys.executable, args=["-m", "idea_to_mvp", "mcp"], env=env, cwd=demo_env)
    async with Client(params) as client:
        assert len((await client.list_tools()).tools) == 5
        assert (await call(client, "list_sessions"))["sessions"] == []
        thread = (await call(client, "start_run", idea=IDEA, autopilot=True))["thread_id"]
        status = await settled(client, thread)
        assert status["gate"]["kind"] == "implement_gate"


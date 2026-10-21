"""MCP server (`idea-to-mvp mcp`, stdio): lets Claude Code, Claude Desktop, or another agent drive the pipeline.

The same graph, checkpointer, and session list as the UI and the headless CLI (`headless.drive`, `read_thread`,
`SessionRegistry`), so a session started through MCP opens in the UI and the other way round; nothing is kept
outside the checkpoint except the progress lines of runs this process started.

A run takes minutes, so `start_run` and `answer_gate` return at once and the work goes on in the server's event
loop; `get_status` says whether it is still running and, once it has stopped, which gate it waits at and what that
gate asks. Every gate is answered by naming it (a stale answer cannot land on the wrong gate), and the two gates
that start agent sessions (`implement_gate`, and `iterate_gate` with a change request) refuse an answer that does
not carry `confirm_spend: true`: the caller is expected to have shown the user the cost estimate first.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections import deque
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from mcp import types
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from idea_to_mvp.config import Settings
from idea_to_mvp.graph import ThreadView, open_graph, read_thread
from idea_to_mvp.headless import (
    HeadlessOptions,
    drive,
    number_answers,
    run_idea,
    session_rows,
    summarize,
)
from idea_to_mvp.nodes.gates import suggested_answers
from idea_to_mvp.resilience import error_hint
from idea_to_mvp.sessions import SessionRegistry

LOGGER = logging.getLogger(__name__)

PROGRESS_KEPT = 200
PROGRESS_SHOWN = 20
# What the caller needs to answer each gate, shown with the gate in `get_status`.
ANSWER_WITH = {
    "answers": 'answers="<one answer per line>" (or use_suggested_answers=true to take the suggestions)',
    "arch_choice": 'option="A" or "B"',
    "plan_gate": "generate=true (false ends the session)",
    "implement_gate": (
        "implement=true with confirm_spend=true, once the user has accepted the cost estimate; "
        "implement=false ends the session without spending anything"
    ),
    "iterate_gate": 'feedback="<changes for the next version>" with confirm_spend=true (it builds and spends again)',
}
# Large payload fields that other tools already serve, or that this server does not act on.
_LEFT_OUT_OF_PAYLOADS = ("report", "publish")

INSTRUCTIONS = (
    "Drives the idea-to-mvp pipeline (panel, gates, blueprint pack, agent implementation, verification). "
    "start_run begins a session and answer_gate answers the gate it stops at; both return at once, so poll "
    "get_status until `running` is false. Show the user the questions, the architecture options, and above all "
    "the cost estimate of the implement gate before you pass confirm_spend: true, which is what starts paid "
    "agent sessions. get_delivery returns the finished report, the project folder, and the zip."
)


class ToolFailure(ToolError):
    """A mistake the caller can fix: its message goes back to the client as the tool's error."""


@dataclass(frozen=True)
class GateAnswer:
    """The arguments of `answer_gate` after the thread and the gate name."""

    answers: str | None = None
    use_suggested_answers: bool = False
    option: str | None = None
    generate: bool | None = None
    implement: bool | None = None
    parallel: int = 0
    harness_tuning: bool | None = None
    feedback: str | None = None
    notes: str = ""
    confirm_spend: bool = False


@dataclass(frozen=True)
class Decision:
    """The resume value for a gate, and whether it starts paid agent sessions."""

    resume: Any
    spends: bool = False


def _needs_confirmation(gate: str, what: str) -> ToolFailure:
    return ToolFailure(
        f"{what} spends money on agent sessions: show the user the estimate in the {gate} gate's payload, "
        "then call again with confirm_spend=true."
    )


def build_resume(payload: Mapping[str, Any], answer: GateAnswer) -> Decision:
    """What the arguments say at the gate `payload` is from, as the resume value the UI form would send.

    Raises `ToolFailure` for a missing or invalid argument, and for a spending answer without `confirm_spend`.
    """
    kind = str(payload.get("kind") or "")
    notes = (answer.notes or "").strip()
    if kind == "answers":
        if answer.answers is not None and (numbered := number_answers(answer.answers)):
            return Decision(numbered)
        if answer.use_suggested_answers:
            if suggested := suggested_answers(payload.get("question_items") or []):
                return Decision(suggested)
            raise ToolFailure("The questions have no suggested answers for every question: pass answers.")
        raise ToolFailure("Pass answers (one per line, numbered or not) or use_suggested_answers=true.")
    if kind == "arch_choice":
        option = (answer.option or "").strip().upper()
        if option not in ("A", "B"):
            raise ToolFailure('Pass option="A" or "B".')
        return Decision({"option": option, "notes": notes})
    if kind == "plan_gate":
        if answer.generate is None:
            raise ToolFailure("Pass generate=true to write the blueprint pack, or false to end the session.")
        return Decision({"generate": answer.generate, "notes": notes})
    if kind == "implement_gate":
        if answer.implement is None:
            raise ToolFailure("Pass implement=true to build (with confirm_spend=true), or false to stop here.")
        if answer.implement and not answer.confirm_spend:
            raise _needs_confirmation("implement_gate", "Building")
        resume: dict[str, Any] = {"implement": answer.implement, "notes": notes, "parallel": max(0, answer.parallel)}
        if answer.harness_tuning is not None:  # left out, the gate's own default stands
            resume["harness_tuning"] = answer.harness_tuning
        return Decision(resume, spends=answer.implement)
    if kind == "iterate_gate":
        feedback = (answer.feedback or "").strip()
        if not feedback:
            raise ToolFailure(
                "Pass feedback describing the changes for the next version. To keep this version, "
                "leave the session as it is: it needs no answer."
            )
        if not answer.confirm_spend:
            raise _needs_confirmation("iterate_gate", "Building another version")
        return Decision({"iterate": True, "feedback": feedback}, spends=True)
    raise ToolFailure(f"unknown gate `{kind}`.")


def _plain(value: Any) -> Any:
    """`value` as plain JSON data (checkpoint payloads can hold paths and other non-JSON objects)."""
    return json.loads(json.dumps(value, default=str))


def _gate_name(kind: str) -> str:
    return f"the {kind.removesuffix('_gate').replace('_', ' ')} gate"


@dataclass
class _Run:
    """A stretch of work this process runs for a session: the background task, what it reported, how it ended."""

    progress: deque[str] = field(default_factory=lambda: deque(maxlen=PROGRESS_KEPT))
    task: asyncio.Task[None] | None = None
    error: str | None = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()


class Orchestrator:
    """The five tools' logic on an open graph; the MCP layer only forwards to it."""

    def __init__(self, graph: CompiledStateGraph, registry: SessionRegistry, settings: Settings) -> None:
        self.graph = graph
        self.registry = registry
        self.settings = settings
        self._runs: dict[str, _Run] = {}

    async def shutdown(self) -> None:
        """Stop the runs in flight; their checkpoints stay, so `idea-to-mvp resume <session>` continues them."""
        tasks = [run.task for run in self._runs.values() if run.task is not None and not run.task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # ---------------------------------------------------------------- tools

    async def start_run(self, idea: str, *, autopilot: bool = False) -> dict[str, Any]:
        idea = idea.strip()
        if not idea:
            raise ToolFailure("The idea is empty: describe the product in a sentence or two.")
        thread_id = str(uuid.uuid4())
        options = HeadlessOptions(autopilot=autopilot)
        run = self._runs[thread_id] = _Run()
        self._launch(
            thread_id,
            run,
            lambda: run_idea(
                self.graph, self.registry, idea, options,
                settings=self.settings, progress=run.progress.append, thread_id=thread_id,
            ),
        )
        return {
            "thread_id": thread_id,
            "state": "running",
            "message": "Started. Poll get_status until `running` is false; it then names the gate to answer.",
        }

    async def get_status(self, thread_id: str) -> dict[str, Any]:
        run = self._runs.get(thread_id)
        view = await read_thread(self.graph, thread_id)
        if not view.values.get("user_idea") and run is None:  # a run of ours that failed at once is still reported
            raise ToolFailure(f"There is no saved session `{thread_id}`: see list_sessions.")
        return self._status(thread_id, view, run)

    async def answer_gate(self, thread_id: str, gate: str, answer: GateAnswer) -> dict[str, Any]:
        run = self._runs.get(thread_id)
        if run and run.running:
            raise ToolFailure("The session is still running: poll get_status until `running` is false.")
        view = await read_thread(self.graph, thread_id)
        if not view.values.get("user_idea"):
            raise ToolFailure(f"There is no saved session `{thread_id}`: see list_sessions.")
        if view.interrupt is None:
            raise ToolFailure("The session waits at no gate: see get_status.")
        kind = str(view.interrupt.get("kind") or "")
        if gate != kind:
            raise ToolFailure(f"The session waits at {_gate_name(kind)} (`{kind}`), not `{gate}`.")
        decision = build_resume(view.interrupt, answer)
        run = self._runs[thread_id] = _Run()
        # the flags-free options stop the run at the next gate: only the caller's answer is passed on
        command: Command[Any] = Command(resume=decision.resume, update={"autopilot": False})
        self._launch(
            thread_id,
            run,
            lambda: drive(
                self.graph, self.registry, thread_id, command, HeadlessOptions(),
                settings=self.settings, progress=run.progress.append,
            ),
        )
        return {"thread_id": thread_id, "state": "running", "answered": kind, "spends": decision.spends}

    async def list_sessions(self, limit: int = 20) -> dict[str, Any]:
        rows = session_rows(self.registry.list(limit=max(1, limit)))
        return {
            "sessions": [{**row, "running": bool((run := self._runs.get(row["thread_id"])) and run.running)} for row in rows]
        }

    async def get_delivery(self, thread_id: str) -> dict[str, Any]:
        run = self._runs.get(thread_id)
        view = await read_thread(self.graph, thread_id)
        values = view.values
        if not values.get("user_idea"):
            raise ToolFailure(f"There is no saved session `{thread_id}`: see list_sessions.")
        if not values.get("delivery_report"):
            raise ToolFailure("The session has not delivered anything yet: see get_status.")
        passed = bool((values.get("verification") or {}).get("passed"))
        return {
            "thread_id": thread_id,
            "idea": str(values.get("user_idea") or ""),
            "iteration": int(values.get("iteration") or 1),
            "verification": "passed" if passed else "failed",
            "report": str(values.get("delivery_report") or ""),
            "workspace": str(values.get("workspace_dir") or ""),
            "delivery_zip": str(values.get("delivery_zip") or ""),
            "blueprint_dir": str(values.get("project_bundle_dir") or ""),
            "publication": _plain(values.get("publication") or None),
            "building_next_version": bool(run and run.running),
        }

    # ---------------------------------------------------------------- inside

    def _launch(self, thread_id: str, run: _Run, work: Any) -> None:
        async def runner() -> None:
            try:
                summary = await work()
                if summary.outcome == "error":
                    run.error = summary.message
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.exception("Session %s failed", thread_id)
                run.error = error_hint("Pipeline", exc)

        run.task = asyncio.create_task(runner(), name=f"idea-to-mvp {thread_id}")

    def _status(self, thread_id: str, view: ThreadView, run: _Run | None) -> dict[str, Any]:
        running = bool(run and run.running)
        summary = summarize(thread_id, view)
        gate = None if running or view.interrupt is None else view.interrupt
        if running:
            state = "running"
        elif gate is None and run and run.error:
            state = "error"
        else:
            state = {
                "verified": "delivered",
                "failed": "delivered",
                "gate": "waiting",
                "ended": "ended",
                "interrupted": "interrupted",
            }.get(summary.outcome, "error")
        status: dict[str, Any] = {
            "thread_id": thread_id,
            "idea": summary.title,
            "state": state,
            "running": running,
            "stage": summary.stage,
            "iteration": summary.iteration,
            "verification": summary.verification,
            "message": summary.message,
            "gate": None,
            "cost": {
                "planning_usd": summary.planning_cost_usd,
                "agent_usd": summary.agent_cost_usd,
                "total_tokens": summary.total_tokens,
                "unpriced_calls": summary.unpriced_calls,
                "estimate": summary.cost_estimate or None,
            },
            "workspace": summary.workspace,
            "delivery_zip": summary.delivery_zip,
            "progress": list(run.progress)[-PROGRESS_SHOWN:] if run else [],
        }
        if gate is not None:
            kind = str(gate.get("kind") or "")
            payload = {key: value for key, value in gate.items() if key not in _LEFT_OUT_OF_PAYLOADS}
            status["gate"] = {"kind": kind, "payload": _plain(payload), "answer_with": ANSWER_WITH.get(kind, "")}
        if state == "error" and run and run.error:
            status["error"] = run.error
        if state in ("error", "interrupted") and summary.next_command:
            status["resume_with"] = summary.next_command
        return status


# ------------------------------------------------------------------ the server


def create_server(settings: Settings) -> MCPServer:
    """The MCP server for `settings`: the graph opens when a client connects and closes when it leaves."""
    holder: list[Orchestrator] = []

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
        async with open_graph(settings) as graph:
            orchestrator = Orchestrator(graph, SessionRegistry(settings.checkpoint_path), settings)
            holder.append(orchestrator)
            try:
                yield
            finally:
                holder.clear()
                await orchestrator.shutdown()

    def orchestrator() -> Orchestrator:
        if not holder:
            raise ToolFailure("The server is not ready.")
        return holder[0]

    server = MCPServer("idea-to-mvp", instructions=INSTRUCTIONS, lifespan=lifespan)
    read_only = types.ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
    writes = types.ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)

    @server.tool(annotations=writes)
    async def start_run(idea: str, autopilot: bool = False) -> dict[str, Any]:
        """Start a session for a product idea. Returns at once with its `thread_id`; poll get_status.

        The panel debates the idea and the session stops at the first gate (the MVP questions). With
        `autopilot` it takes the suggested answers, the recommended architecture, and writes the blueprint
        pack, then stops at the implement gate. It never starts agent sessions: that is answer_gate on the
        implement gate, with confirm_spend.
        """
        return await orchestrator().start_run(idea, autopilot=autopilot)

    @server.tool(annotations=read_only)
    async def get_status(thread_id: str) -> dict[str, Any]:
        """Where a session is: `state` (running, waiting, delivered, ended, interrupted, error), the stage,
        the cost so far, the last progress lines, and, when it waits at a gate, `gate` with the gate's
        `kind`, its `payload` (the questions, the architecture options, the implement gate's cost estimate)
        and how to answer it."""
        return await orchestrator().get_status(thread_id)

    @server.tool(annotations=writes)
    async def answer_gate(
        thread_id: str,
        gate: str,
        answers: str | None = None,
        use_suggested_answers: bool = False,
        option: str | None = None,
        generate: bool | None = None,
        implement: bool | None = None,
        parallel: int = 0,
        harness_tuning: bool | None = None,
        feedback: str | None = None,
        notes: str = "",
        confirm_spend: bool = False,
    ) -> dict[str, Any]:
        """Answer the gate a session waits at (name it in `gate`), then poll get_status.

        answers: `answers` (one per line) or `use_suggested_answers`. arch_choice: `option` "A" or "B".
        plan_gate: `generate`. implement_gate: `implement` (and optionally `parallel`, `harness_tuning`).
        iterate_gate: `feedback` for the next version. `notes` is free text for any gate.

        `confirm_spend` must be true to build (implement_gate with implement=true) or to build another
        version (iterate_gate): those start agent sessions that cost money. Show the user the estimate in
        the gate's payload first.
        """
        answer = GateAnswer(
            answers=answers, use_suggested_answers=use_suggested_answers, option=option, generate=generate,
            implement=implement, parallel=parallel, harness_tuning=harness_tuning, feedback=feedback, notes=notes,
            confirm_spend=confirm_spend,
        )
        return await orchestrator().answer_gate(thread_id, gate, answer)

    @server.tool(annotations=read_only)
    async def list_sessions(limit: int = 20) -> dict[str, Any]:
        """The saved sessions (from the UI, the CLI, or this server), newest first, with the gate each waits at."""
        return await orchestrator().list_sessions(limit)

    @server.tool(annotations=read_only)
    async def get_delivery(thread_id: str) -> dict[str, Any]:
        """A delivered session's report, verdict (passed or failed), project folder, blueprint folder, and zip."""
        return await orchestrator().get_delivery(thread_id)

    return server


def serve() -> int:
    """Run the server on stdin / stdout until the client leaves. Logs and progress go to stderr, never stdout."""
    from idea_to_mvp.config import get_settings
    from idea_to_mvp.observability import apply_tracing_env

    apply_tracing_env()
    settings = get_settings()
    if settings.demo_mode:
        LOGGER.warning("demo mode: canned models and a stand-in implementation, no API calls")
    create_server(settings).run()
    return 0

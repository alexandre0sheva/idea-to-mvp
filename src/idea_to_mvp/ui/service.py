from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import gradio as gr
from langgraph.types import Command

from idea_to_mvp.config import PanelMode, Settings
from idea_to_mvp.exporter import save_session_export
from idea_to_mvp.graph import GraphProvider, merged_values, pending_interrupt, run_config
from idea_to_mvp.implementation.events import EVENT_KINDS, ImplEvent
from idea_to_mvp.implementation.progress import TaskResult, load_progress, spent_in_iteration
from idea_to_mvp.implementation.workspace import diff_stat
from idea_to_mvp.nodes.common import stated_preferences
from idea_to_mvp.nodes.implement import builds_task_by_task, effective_parallel
from idea_to_mvp.plan import load_plan
from idea_to_mvp.sessions import SessionRegistry
from idea_to_mvp.state import make_initial_state
from idea_to_mvp.ui.artifacts import (
    FileView,
    artifact_key,
    blueprint_zip,
    read_workspace_file,
)
from idea_to_mvp.ui.components import stage_header
from idea_to_mvp.ui.console import (
    render_console,
    render_cost_meter,
    render_task_board,
    running_tasks,
)
from idea_to_mvp.ui.dashboard import render_dashboard
from idea_to_mvp.ui.gates import GATES, gate_outputs
from idea_to_mvp.ui.render import (
    architect_block,
    moderator_banner,
    openings_row,
    questions_block,
    research_card,
    summary_block,
    thinking_block,
    turn_block,
    warning_block,
)
from idea_to_mvp.ui.view import (
    MODE_DONE,
    MODE_IDEA,
    MODE_INTERRUPTED,
    USER_KINDS,
    LiveTurns,
    StageMark,
    TurnInfo,
    ViewEntry,
    implementation_progress,
    mode_from_state,
    panel_token,
    running_from_state,
    stage_elapsed,
    stage_for_view,
    stage_marks,
    stage_statuses,
    status_from_state,
    transcript_from_state,
)
from idea_to_mvp.usage import summarize_usage

LOGGER = logging.getLogger(__name__)

__all__ = ["AppContext", "SubmitService"]

_TITLE_LENGTH = 70
_LOG_LIMIT = 2000  # events kept per thread for the console
_PROGRESS_INTERVAL_SECONDS = 0.5  # tool events can arrive in bursts; task boundaries always render
_TOKEN_INTERVAL_SECONDS = 0.15  # panel tokens arrive far faster than the chat is worth re-rendering


@dataclass(frozen=True)
class AppContext:
    settings: Settings
    graphs: GraphProvider
    registry: SessionRegistry | None = None

    @property
    def sessions(self) -> SessionRegistry:
        return self.registry or SessionRegistry(self.settings.checkpoint_path)


def _error_hint(stage: str, exc: Exception) -> str:
    name = type(exc).__name__.lower()
    if "authentication" in name or "permission" in name or "unauthorized" in name:
        return f"{stage} failed due to API authentication/permission. Check provider keys and model access."
    if "ratelimit" in name or "rate_limit" in name:
        return f"{stage} hit a rate limit. Retry shortly or lower request frequency."
    if "timeout" in name:
        return f"{stage} timed out. Retry with fewer rounds or try another model."
    return f"{stage} failed with `{type(exc).__name__}`: {exc}"


def _chat_message(entry: ViewEntry, elapsed: dict[str, float] | None = None) -> dict[str, str]:
    """Map one derived view entry to a Gradio chat message."""
    if entry.kind == "delivery_report" and isinstance(entry.data, dict):
        return {"role": "assistant", "content": render_dashboard(entry.data, elapsed=elapsed)}
    if entry.kind in USER_KINDS:
        return {"role": "user", "content": entry.content}
    if entry.kind == "research" and isinstance(entry.data, dict):
        return {"role": "assistant", "content": research_card(entry.data) or turn_block(entry.speaker, entry.content)}
    if entry.kind == "moderator":
        return {"role": "assistant", "content": moderator_banner(entry.content)}
    if entry.kind == "discussion" and isinstance(entry.data, TurnInfo):
        block = turn_block(entry.speaker, entry.content, collapsed=entry.data.collapsed, live=entry.data.live)
        return {"role": "assistant", "content": block}
    if entry.kind == "thinking":
        return {"role": "assistant", "content": thinking_block(entry.speaker, entry.content)}
    if entry.kind == "summary":
        return {"role": "assistant", "content": summary_block(entry.content)}
    if entry.kind == "architect":
        return {"role": "assistant", "content": architect_block(entry.content)}
    if entry.kind == "questions":
        block = questions_block(entry.content.splitlines(), entry.data or None)
        return {"role": "assistant", "content": block or turn_block("Questions", entry.content)}
    if entry.kind == "warning":
        return {"role": "assistant", "content": warning_block(entry.speaker, entry.content)}
    if entry.kind == "system":
        return {"role": "assistant", "content": f"### {entry.speaker}\n\n{entry.content}"}
    return {"role": "assistant", "content": turn_block(entry.speaker, entry.content)}


def _chat_messages(entries: list[ViewEntry], elapsed: dict[str, float] | None = None) -> list[dict[str, str]]:
    """The chat for a transcript: like `_chat_message` per entry, except that the opening statements, which were
    written at the same time, share one message laid out as a row."""
    messages: list[dict[str, str]] = []
    openings: list[ViewEntry] = []

    def flush() -> None:
        if openings:
            infos = [cast(TurnInfo, e.data) for e in openings]
            row = openings_row(
                [(e.speaker, e.content) for e in openings],
                collapsed=all(info.collapsed for info in infos),
                live=[info.live for info in infos],
            )
            messages.append({"role": "assistant", "content": row})
            openings.clear()

    for entry in entries:
        if entry.kind == "discussion" and isinstance(entry.data, TurnInfo) and entry.data.phase == "opening":
            openings.append(entry)
            continue
        flush()
        messages.append(_chat_message(entry, elapsed))
    flush()
    return messages


def _main_widgets(mode: str) -> tuple[str, str, str]:
    """(label, placeholder, button label) of the idea box and run button, which only serve a new idea and Continue;
    every gate has its own form (`ui/gates.py`)."""
    if mode == MODE_INTERRUPTED:
        return "Run interrupted", "Press Continue to resume this run.", "Continue"
    if mode == MODE_DONE:
        return "Describe your next idea", "Describe your product idea...", "Run discussion"
    return "Describe your idea", "Describe your product idea...", "Run discussion"


class SubmitService:
    """Bridges Gradio events to the checkpointed graph.

    The UI is a projection of graph state: every render re-reads the thread's checkpoint and derives
    the chat, mode, and status from it (`ui/view.py`). The only thing the browser session keeps is
    the thread id, so any session can be rebuilt after a restart.
    """

    def __init__(self, context: AppContext):
        self._context = context
        # Checkpoint times per thread (marks, not the derived seconds): reading the history is the costly part,
        # so it is redone only after the graph committed a step, not on every progress render.
        self._marks: dict[str, list[StageMark]] = {}
        # The live implementation view. Events are transient by nature (they are not in the checkpoint): they
        # live here for the life of the process, per thread; everything else on the board is read from disk.
        self._events: dict[str, list[ImplEvent]] = {}
        self._active: set[str] = set()  # threads with a run streaming right now
        self._live: dict[str, LiveTurns] = {}  # the panel turns being written, per streaming thread
        self._diffs: dict[tuple[str, str], str] = {}  # (workspace, commit) -> git diff --stat (commits never change)
        self._tab: dict[str, str] = {}  # the tab last selected for each thread
        self._artifact_seen: dict[str, tuple[str, str, int, str]] = {}  # what the artifacts panel currently shows

    def _reset_view(self, thread_id: str) -> None:
        """A page that (re)loads a thread starts on the conversation tab with an empty artifacts panel."""
        self._tab[thread_id] = "conversation"
        self._artifact_seen.pop(thread_id, None)

    async def _elapsed(self, thread_id: str, values: dict[str, Any], active: str, *, waiting: bool) -> dict[str, float]:
        """Seconds of work per step, derived from the thread's checkpoint times (see `view.stage_elapsed`)."""
        if not values.get("user_idea"):
            return {}
        if thread_id not in self._marks:
            graph = await self._context.graphs.get()
            history = [s async for s in graph.aget_state_history({"configurable": {"thread_id": thread_id}})]
            self._marks[thread_id] = stage_marks(history)
        return stage_elapsed(self._marks[thread_id], now=None if waiting else time.time(), active=active)

    # -------------------------------------------------------------- rendering

    async def _read(self, thread_id: str) -> tuple[dict[str, Any], dict[str, Any] | None, tuple[str, ...]]:
        graph = await self._context.graphs.get()
        # subgraphs=True: while the panel runs, its turns live in the subgraph's checkpoint, not the parent's.
        snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}}, subgraphs=True)
        return merged_values(snapshot), pending_interrupt(snapshot), tuple(snapshot.next or ())

    async def _sessions_update(self, current: str | None) -> Any:
        sessions = self._context.sessions.list()
        choices = [
            (
                f"{s.title} · {'waiting: ' + s.interrupted_at if s.interrupted_at else s.stage}"
                f" · {s.updated_at.astimezone():%m-%d %H:%M}",
                s.thread_id,
            )
            for s in sessions
        ]
        value = current if current in {c[1] for c in choices} else None
        return gr.update(choices=choices, value=value)

    async def _render(
        self,
        thread_id: str,
        *,
        status: str | None = None,
        clear_input: bool = False,
        running: tuple[str, str] | None = None,
        pending_user: ViewEntry | None = None,
        live: list[ViewEntry] | None = None,
        error: str | None = None,
        idea_title: str | None = None,
    ) -> tuple[Any, ...]:
        values, interrupt_payload, next_nodes = await self._read(thread_id)
        mode = mode_from_state(values, interrupt_payload, next_nodes=next_nodes)
        overlay = running or (
            running_from_state(values, next_nodes) if interrupt_payload is None and mode == MODE_INTERRUPTED else None
        )
        entries = transcript_from_state(
            values, interrupt_payload, running=overlay, pending_user=pending_user, live=live or (), error=error
        )
        stage = stage_for_view(values, mode)
        elapsed = await self._elapsed(thread_id, values, stage, waiting=interrupt_payload is not None)
        title = str(values.get("user_idea") or idea_title or "").strip()
        if title:
            self._context.sessions.upsert(
                thread_id,
                title[:_TITLE_LENGTH],
                str(values.get("stage") or "discussion"),
                interrupted_at=(interrupt_payload or {}).get("kind"),
            )
        return await self._pack(
            status=status if status is not None else status_from_state(values, mode, overlay),
            mode=mode,
            thread_id=thread_id,
            chat=_chat_messages(entries, elapsed),
            stage=stage,
            clear_input=clear_input,
            values=values,
            elapsed=elapsed,
            implementation=await self._implementation_view(thread_id, values, stage),
            # the form of the pending gate, except while a decision is being sent or a step is running
            gate=(str(interrupt_payload["kind"]), interrupt_payload)
            if interrupt_payload and pending_user is None and running is None
            else None,
        )

    async def _pack(
        self,
        *,
        status: str,
        mode: str,
        thread_id: str,
        chat: list[dict[str, str]],
        stage: str,
        clear_input: bool = False,
        values: dict[str, Any] | None = None,
        elapsed: dict[str, float] | None = None,
        gate: tuple[str, dict[str, Any]] | None = None,
        implementation: tuple[Any, ...] | None = None,
    ) -> tuple[Any, ...]:
        """One tuple of updates for the app's outputs: the shared widgets, the implementation view (tab, board,
        console, meter, artifacts), then the gate forms (`gate` is the pending gate to show, None hides them all)."""
        new_idea = mode in (MODE_IDEA, MODE_DONE)
        label, placeholder, button = _main_widgets(mode)
        input_update = (
            gr.update(value="", label=label, placeholder=placeholder, visible=new_idea)
            if clear_input
            else gr.update(label=label, placeholder=placeholder, visible=new_idea)
        )
        return (
            gr.update(value=status),
            chat,
            input_update,
            gr.update(visible=new_idea),
            gr.update(value=button, visible=new_idea or mode == MODE_INTERRUPTED),
            thread_id,
            gr.update(
                value=stage_header(
                    stage,
                    stage_statuses(values or {}),
                    elapsed or {},
                    summarize_usage((values or {}).get("usage") or []),
                )
            ),
            await self._sessions_update(thread_id),
            gr.update(visible=new_idea),  # the example ideas only make sense for a new idea
            *(implementation if implementation is not None else self._blank_implementation()),
            *gate_outputs(gate),
        )

    # ------------------------------------------------- implementation view

    def _blank_implementation(self) -> tuple[Any, ...]:
        """The implementation view of a session that has not started building (a new or cleared session)."""
        return (
            gr.update(selected="conversation"),
            gr.update(value=render_task_board({}, {}, set())),
            gr.update(value=render_console([])),
            gr.update(value=render_cost_meter(0.0, self._context.settings.implementer_max_total_usd)),
            gr.update(value=""),
            gr.update(value=None, visible=False),
            gr.update(value=None, visible=False),
            "",  # the explorer's workspace: none
        )

    async def _implementation_view(self, thread_id: str, values: dict[str, Any], stage: str) -> tuple[Any, ...]:
        settings = self._context.settings
        events = self._events.get(thread_id, [])
        workspace_dir = str(values.get("workspace_dir") or "")
        workspace = Path(workspace_dir) if workspace_dir and Path(workspace_dir).is_dir() else None
        plan: dict[str, Any] = {}
        results: dict[str, TaskResult] = {}
        diffs: dict[str, str] = {}
        if workspace is not None:
            loaded = load_plan(_read_text(workspace / "plan.json"))
            plan = loaded.model_dump() if loaded else {}
            results = {**(values.get("task_results") or {}), **load_progress(workspace)}  # the file is fresher mid-run
            diffs = await self._diff_stats(workspace, results)
        running = running_tasks(events) if thread_id in self._active else set()
        parallel = (
            workspace is not None
            and (workspace / ".git").is_dir()
            and builds_task_by_task(cast(Any, values))
            and effective_parallel(cast(Any, values), settings) > 1
        )
        iteration = int(values.get("iteration") or 1)
        usage_spent = (summarize_usage(values.get("usage") or [])["cost_usd"] or 0.0) - float(
            values.get("spent_before_iteration") or 0.0
        )
        spent = max(spent_in_iteration(results, iteration), usage_spent)  # progress is live, usage catches lanes/fixes
        return (
            self._tab_update(thread_id, str(values.get("stage") or "")),  # the state's own stage: the view's flickers
            gr.update(value=render_task_board(plan, results, running, diffs=diffs, parallel=bool(parallel))),
            gr.update(value=render_console(events)),
            gr.update(value=render_cost_meter(spent, settings.implementer_max_total_usd)),
            *await self._artifact_updates(thread_id, values, workspace),
        )

    async def _diff_stats(self, workspace: Path, results: dict[str, TaskResult]) -> dict[str, str]:
        found: dict[str, str] = {}
        for result in results.values():
            commit = result["commit"]
            if result["status"] != "done" or not commit:
                continue
            key = (str(workspace), commit)
            if key not in self._diffs:
                self._diffs[key] = await asyncio.to_thread(diff_stat, workspace, commit)
            found[commit] = self._diffs[key]
        return found

    def _tab_update(self, thread_id: str, stage: str) -> Any:
        """Show the implementation tab while a run is building and verifying, the conversation otherwise; the
        selection is only sent when it changes, so the user can look at the other tab meanwhile. (`stage` is the
        state's own: the view's derived stage briefly reads "done" between the steps of a subgraph.)"""
        wanted = "implementation" if thread_id in self._active and stage in ("implementation", "verification", "report") else "conversation"
        if self._tab.get(thread_id, "conversation") == wanted:
            return gr.update()
        self._tab[thread_id] = wanted
        return gr.update(selected=wanted)

    async def _artifact_updates(self, thread_id: str, values: dict[str, Any], workspace: Path | None) -> tuple[Any, ...]:
        """The artifacts panel (path, the two downloads, the file explorer), refreshed only when what it shows changes."""
        bundle_dir = str(values.get("project_bundle_dir") or "")
        archive = str(values.get("delivery_zip") or "")
        key = artifact_key(str(workspace or ""), bundle_dir if Path(bundle_dir).is_dir() else "", archive)
        if self._artifact_seen.get(thread_id) == key:
            return (gr.update(), gr.update(), gr.update(), gr.update())
        self._artifact_seen[thread_id] = key
        blueprint = ""
        if key[1]:
            blueprint = str(await asyncio.to_thread(blueprint_zip, bundle_dir, self._context.settings.deliveries_dir))
        return (
            gr.update(value=str(workspace or "")),
            gr.update(value=archive or None, visible=bool(archive) and Path(archive).is_file()),
            gr.update(value=blueprint or None, visible=bool(blueprint)),
            str(workspace or ""),  # the state the file explorer is (re)built from: its root cannot change in place
        )

    async def view_file(self, thread_id: str, selection: Any) -> Any:
        """The file picked in the explorer, read-only, and only from this thread's workspace."""
        values, _interrupt, _next = await self._read((thread_id or "").strip())
        workspace = str(values.get("workspace_dir") or "")
        view = read_workspace_file(workspace, selection) if workspace else FileView()
        return gr.update(value=view.text, language=view.language, label=view.note or "Selected file")

    # ------------------------------------------------------------ sessions UI

    async def clear_session(self) -> tuple[Any, ...]:
        return await self._pack(
            status="",
            mode=MODE_IDEA,
            thread_id=str(uuid.uuid4()),
            chat=[],
            stage="discussion",
            clear_input=True,
        )

    async def initial_view(self, thread_id: str) -> tuple[Any, ...]:
        """Page-load render: restores the browser's current thread and refreshes the session list."""
        thread_id = thread_id or str(uuid.uuid4())
        self._reset_view(thread_id)
        return await self._render(thread_id)

    async def load_session(self, selected_thread_id: str | None, current_thread_id: str) -> tuple[Any, ...]:
        if not selected_thread_id:
            return await self._render(current_thread_id, status="Pick a saved session first.")
        self._reset_view(selected_thread_id)
        return await self._render(selected_thread_id, clear_input=True)

    async def delete_session(self, selected_thread_id: str | None, current_thread_id: str) -> tuple[Any, ...]:
        if not selected_thread_id:
            return await self._render(current_thread_id, status="Pick a saved session first.")
        graph = await self._context.graphs.get()
        if graph.checkpointer is not None and hasattr(graph.checkpointer, "adelete_thread"):
            await graph.checkpointer.adelete_thread(selected_thread_id)
        self._context.sessions.delete(selected_thread_id)
        if selected_thread_id == current_thread_id:
            return await self.clear_session()
        return await self._render(current_thread_id, status="Session deleted.")

    # ----------------------------------------------------------------- submit

    async def handle_submit(
        self,
        user_text: str,
        rounds: int,
        thread_id: str,
        panel_mode: str | None = None,
        autopilot: bool = False,
        gate_inputs: dict[str, Any] | None = None,
        preferences: dict[str, Any] | None = None,
    ) -> AsyncGenerator[tuple[Any, ...], None]:
        text = (user_text or "").strip()
        autopilot = bool(autopilot)
        thread_id = (thread_id or "").strip() or str(uuid.uuid4())
        values, interrupt_payload, next_nodes = await self._read(thread_id)
        mode = mode_from_state(values, interrupt_payload, next_nodes=next_nodes)

        payload: Any
        pending_user: ViewEntry | None = None
        running: tuple[str, str] | None = None
        idea_title: str | None = None

        if mode in (MODE_IDEA, MODE_DONE):
            if not text:
                yield await self._render(thread_id, status="Please describe your idea first.")
                return
            if mode == MODE_DONE:
                thread_id = str(uuid.uuid4())
            payload = make_initial_state(
                text,
                int(rounds),
                panel_mode=cast(PanelMode, panel_mode or self._context.settings.panel_mode),
                autopilot=autopilot,
                preferences=stated_preferences(preferences),
            )
            pending_user = ViewEntry("idea", "You", text)
            running = running_from_state(payload, ("research" if self._context.settings.enable_research else "panel",))
            idea_title = text
            values = {}
        elif (kind := str((interrupt_payload or {}).get("kind") or "")) in GATES:
            spec = GATES[kind]
            inputs = dict(gate_inputs or {})
            if problem := spec.validate(inputs, interrupt_payload or {}):
                yield await self._render(thread_id, status=problem)
                return
            resume = spec.build_resume(inputs)
            payload = Command(resume=resume, update={"autopilot": autopilot})
            pending_user = spec.echo(inputs)
            step = spec.next_step(resume)
            running = running_from_state(values, (step,)) if step else None
        else:  # MODE_INTERRUPTED: continue from the last checkpoint
            payload = None
            running = running_from_state(values, next_nodes)
            if bool(values.get("autopilot")) != autopilot:  # the toggle changed while the run was stopped
                graph = await self._context.graphs.get()
                await graph.aupdate_state({"configurable": {"thread_id": thread_id}}, {"autopilot": autopilot})

        if pending_user is not None and running is not None and mode in (MODE_IDEA, MODE_DONE):
            # New thread: nothing recorded yet, so draw the overlay alone.
            yield await self._pack(
                status="Running panel discussion...",
                mode=MODE_IDEA,
                thread_id=thread_id,
                chat=[_chat_message(pending_user), _chat_message(ViewEntry("thinking", running[0], running[1]))],
                stage="discussion",
                clear_input=True,
            )
        else:
            yield await self._render(
                thread_id,
                status=running[1] if running else "Working...",
                clear_input=True,
                running=running,
                pending_user=pending_user,
            )

        graph = await self._context.graphs.get()
        config = run_config(thread_id, max_concurrency=self._context.settings.llm_max_concurrency)
        console: list[ImplEvent] = []  # this run's events (the status line's progress); `_events` keeps the log
        live = self._live[thread_id] = LiveTurns()
        last_token_render = 0.0
        history = self._events.setdefault(thread_id, [])
        last_progress = 0.0
        self._active.add(thread_id)
        try:
            # subgraphs=True: updates from inside the panel subgraph arrive too, so the chat fills turn by turn;
            # "custom" carries the live implementation events; "messages" the panel's tokens.
            async for _namespace, stream_mode, data in graph.astream(
                payload, config=config, stream_mode=["updates", "custom", "messages"], subgraphs=True
            ):
                if stream_mode == "messages":
                    if token := panel_token(data):
                        live.add(*token)
                        now = time.monotonic()
                        if now - last_token_render >= _TOKEN_INTERVAL_SECONDS:
                            last_token_render = now
                            yield await self._render(
                                thread_id, status=_writing(live), live=live.entries(), idea_title=idea_title
                            )
                    continue
                if stream_mode != "custom":
                    self._marks.pop(thread_id, None)  # a step was committed: its checkpoint time is new
                    self._settle_live(live, data)
                    yield await self._render(thread_id, live=live.entries(), idea_title=idea_title)
                    continue
                if not isinstance(data, dict) or data.get("kind") not in EVENT_KINDS:
                    continue
                console.append(data)  # type: ignore[arg-type]
                history.append(data)  # type: ignore[arg-type]
                del history[:-_LOG_LIMIT]
                now = time.monotonic()
                if data["kind"] in ("task_start", "task_end") or now - last_progress >= _PROGRESS_INTERVAL_SECONDS:
                    last_progress = now
                    status, detail = implementation_progress(
                        console, total_budget=self._context.settings.implementer_max_total_usd
                    )
                    yield await self._render(
                        thread_id, status=status or None, running=("Implementer", detail or status), idea_title=idea_title
                    )
        except Exception as exc:
            LOGGER.exception("Pipeline run failed")
            hint = _error_hint("Pipeline", exc)
            yield await self._render(
                thread_id,
                status="Run failed. See the error in chat; you can retry or continue.",
                error=hint,
                idea_title=idea_title,
            )
            return
        else:
            yield await self._render(thread_id, idea_title=idea_title)
        finally:
            self._active.discard(thread_id)
            self._live.pop(thread_id, None)

    @staticmethod
    def _settle_live(live: LiveTurns, update: Any) -> None:
        """Fold a committed step into the live panel turns: a finished opening replaces its tokens (the openings
        only reach the transcript together, at the merge); a committed turn, merge, or step is in the
        transcript now, so nothing is live any more."""
        if not isinstance(update, dict):
            return
        if isinstance(openings := (update.get("opening_turn") or {}).get("opening_turns"), dict):
            for speaker, text in openings.items():
                live.finish(str(speaker), str(text))
        if update.keys() - {"opening_turn"}:
            live.clear()

    async def gate_payload(self, thread_id: str) -> dict[str, Any]:
        """The payload of the gate the thread is paused at ({} if it is not at one): what a gate's own events
        (fill the suggestions, save a blueprint file) work from."""
        _values, interrupt_payload, _next = await self._read((thread_id or "").strip())
        return dict(interrupt_payload or {})

    async def stop_run(self, thread_id: str) -> tuple[Any, ...]:
        """The Stop button. Gradio cancels the running `handle_submit` (its `cancels=` wiring), which cancels
        the graph run between checkpoints; this re-renders from that checkpoint. A stopped run is `interrupted`
        (the step that was running is redone by Continue); at a gate nothing changes."""
        thread_id = (thread_id or "").strip()
        self._marks.pop(thread_id, None)
        self._active.discard(thread_id)  # its run is being cancelled: nothing on the board is running any more
        values, interrupt_payload, next_nodes = await self._read(thread_id)
        if mode_from_state(values, interrupt_payload, next_nodes=next_nodes) != MODE_INTERRUPTED:
            return await self._render(thread_id)
        return await self._render(
            thread_id,
            status="Stopped. Progress is saved; press Continue to resume from the last checkpoint.",
        )

    # ----------------------------------------------------------------- export

    async def save_conversation(self, user_text: str, thread_id: str) -> tuple[Any, Any]:
        draft_input = (user_text or "").strip()
        values, interrupt_payload, next_nodes = await self._read(thread_id)
        entries = transcript_from_state(values, interrupt_payload)
        if not entries and not draft_input:
            return gr.update(value="Nothing to save yet."), gr.update(value=None, visible=False)
        mode = mode_from_state(values, interrupt_payload, next_nodes=next_nodes)
        try:
            export_paths = save_session_export(
                exports_dir=self._context.settings.exports_dir,
                entries=entries,
                thread_id=thread_id,
                mode=mode,
                draft_input=draft_input,
            )
        except Exception as exc:
            LOGGER.exception("Conversation export failed")
            return (
                gr.update(value=_error_hint("Conversation export", exc)),
                gr.update(value=None, visible=False),
            )
        return (
            gr.update(value=f"Saved conversation to `{export_paths[0]}` (and `.json`)."),
            gr.update(value=[str(path) for path in export_paths], visible=True),
        )


def _writing(live: LiveTurns) -> str:
    names = [entry.speaker for entry in live.entries() if cast(TurnInfo, entry.data).live]
    return f"{', '.join(names)} {'is' if len(names) == 1 else 'are'} writing..." if names else "Panel discussion..."


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

import gradio as gr
from langgraph.types import Command

from idea_to_mvp.config import Settings
from idea_to_mvp.exporter import save_session_markdown
from idea_to_mvp.graph import GraphProvider, pending_interrupt, run_config
from idea_to_mvp.roles import SPEAKER_ORDER
from idea_to_mvp.sessions import SessionRegistry
from idea_to_mvp.state import make_initial_state
from idea_to_mvp.ui.render import (
    architect_block,
    questions_block,
    stage_tracker,
    summary_block,
    thinking_block,
    turn_block,
)
from idea_to_mvp.ui.view import (
    ARCH_CHOICE_A,
    ARCH_CHOICE_B,
    IMPL_CHOICE_SKIP,
    IMPL_CHOICE_START,
    MODE_ANSWERS,
    MODE_ARCH_CHOICE,
    MODE_DONE,
    MODE_IDEA,
    MODE_IMPL_GATE,
    MODE_INTERRUPTED,
    MODE_PLAN_GATE,
    PLAN_CHOICE_GENERATE,
    PLAN_CHOICE_SKIP,
    USER_KINDS,
    ViewEntry,
    mode_from_state,
    running_from_state,
    stage_for_view,
    status_from_state,
    transcript_from_state,
)
from idea_to_mvp.usage import format_usage

LOGGER = logging.getLogger(__name__)

__all__ = [
    "ARCH_CHOICE_A",
    "ARCH_CHOICE_B",
    "IMPL_CHOICE_SKIP",
    "IMPL_CHOICE_START",
    "MODE_ANSWERS",
    "MODE_ARCH_CHOICE",
    "MODE_DONE",
    "MODE_IDEA",
    "MODE_IMPL_GATE",
    "MODE_INTERRUPTED",
    "MODE_PLAN_GATE",
    "PLAN_CHOICE_GENERATE",
    "PLAN_CHOICE_SKIP",
    "AppContext",
    "SubmitService",
]

# Decision-radio configuration per gate mode: (choices, default value).
DECISION_CHOICES: dict[str, tuple[list[str], str]] = {
    MODE_ARCH_CHOICE: ([ARCH_CHOICE_A, ARCH_CHOICE_B], ARCH_CHOICE_A),
    MODE_PLAN_GATE: ([PLAN_CHOICE_GENERATE, PLAN_CHOICE_SKIP], PLAN_CHOICE_GENERATE),
    MODE_IMPL_GATE: ([IMPL_CHOICE_START, IMPL_CHOICE_SKIP], IMPL_CHOICE_START),
}

_INPUT_LABELS: dict[str, tuple[str, str]] = {
    MODE_IDEA: ("Describe your idea", "Describe your product idea..."),
    MODE_ANSWERS: ("Your answers to MVP decision questions", "1. ...\n2. ..."),
    MODE_ARCH_CHOICE: ("Notes on the architecture choice (optional)", "Constraints or preferences..."),
    MODE_PLAN_GATE: ("Planning notes (optional)", "Anything the planner should account for..."),
    MODE_IMPL_GATE: ("Notes for the implementation agents (optional)", "Priorities, stack preferences..."),
    MODE_INTERRUPTED: ("Run interrupted", "Press Continue to resume this run."),
    MODE_DONE: ("Describe your next idea", "Describe your product idea..."),
}
_BUTTON_LABELS: dict[str, str] = {
    MODE_IDEA: "Run discussion",
    MODE_ANSWERS: "Submit answers",
    MODE_ARCH_CHOICE: "Send choice",
    MODE_PLAN_GATE: "Send decision",
    MODE_IMPL_GATE: "Send decision",
    MODE_INTERRUPTED: "Continue",
    MODE_DONE: "Run discussion",
}

_TITLE_LENGTH = 70


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


def _with_usage_line(status: str, values: dict[str, Any]) -> str:
    usage = format_usage(values.get("usage") or [])
    return f"{status}\n\n`{usage}`".strip() if usage else status


def _chat_message(entry: ViewEntry) -> dict[str, str]:
    """Map one derived view entry to a Gradio chat message."""
    if entry.kind in USER_KINDS:
        return {"role": "user", "content": entry.content}
    if entry.kind == "thinking":
        return {"role": "assistant", "content": thinking_block(entry.speaker, entry.content)}
    if entry.kind == "summary":
        return {"role": "assistant", "content": summary_block(entry.content)}
    if entry.kind == "architect":
        return {"role": "assistant", "content": architect_block(entry.content)}
    if entry.kind == "questions":
        block = questions_block(entry.content.splitlines(), entry.data or None)
        return {"role": "assistant", "content": block or turn_block("Questions", entry.content)}
    if entry.kind == "system":
        return {"role": "assistant", "content": f"### {entry.speaker}\n\n{entry.content}"}
    return {"role": "assistant", "content": turn_block(entry.speaker, entry.content)}


class SubmitService:
    """Bridges Gradio events to the checkpointed graph.

    The UI is a projection of graph state: every render re-reads the thread's checkpoint and derives
    the chat, mode, and status from it (`ui/view.py`). The only thing the browser session keeps is
    the thread id, so any session can be rebuilt after a restart.
    """

    def __init__(self, context: AppContext):
        self._context = context

    # -------------------------------------------------------------- rendering

    async def _read(self, thread_id: str) -> tuple[dict[str, Any], dict[str, Any] | None, tuple[str, ...]]:
        graph = await self._context.graphs.get()
        snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
        return dict(snapshot.values or {}), pending_interrupt(snapshot), tuple(snapshot.next or ())

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
        error: str | None = None,
        idea_title: str | None = None,
    ) -> tuple[Any, ...]:
        values, interrupt_payload, next_nodes = await self._read(thread_id)
        mode = mode_from_state(values, interrupt_payload, next_nodes=next_nodes)
        overlay = running or (
            running_from_state(values, next_nodes) if interrupt_payload is None and mode == MODE_INTERRUPTED else None
        )
        entries = transcript_from_state(
            values, interrupt_payload, running=overlay, pending_user=pending_user, error=error
        )
        title = str(values.get("user_idea") or idea_title or "").strip()
        if title:
            self._context.sessions.upsert(
                thread_id,
                title[:_TITLE_LENGTH],
                str(values.get("stage") or "discussion"),
                interrupted_at=(interrupt_payload or {}).get("kind"),
            )
        return await self._pack(
            status=_with_usage_line(
                status if status is not None else status_from_state(values, mode, overlay), values
            ),
            mode=mode,
            thread_id=thread_id,
            chat=[_chat_message(e) for e in entries],
            stage=stage_for_view(values, mode),
            clear_input=clear_input,
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
    ) -> tuple[Any, ...]:
        label, placeholder = _INPUT_LABELS.get(mode, _INPUT_LABELS[MODE_IDEA])
        input_update = (
            gr.update(value="", label=label, placeholder=placeholder)
            if clear_input
            else gr.update(label=label, placeholder=placeholder)
        )
        if mode in DECISION_CHOICES:
            choices, default = DECISION_CHOICES[mode]
            decision_update = gr.update(visible=True, choices=choices, value=default)
        else:
            decision_update = gr.update(visible=False)
        return (
            gr.update(value=status),
            chat,
            input_update,
            gr.update(visible=mode in (MODE_IDEA, MODE_DONE)),
            decision_update,
            gr.update(value=_BUTTON_LABELS.get(mode, _BUTTON_LABELS[MODE_IDEA])),
            thread_id,
            gr.update(value=stage_tracker(stage)),
            await self._sessions_update(thread_id),
        )

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
        return await self._render(thread_id or str(uuid.uuid4()))

    async def load_session(self, selected_thread_id: str | None, current_thread_id: str) -> tuple[Any, ...]:
        if not selected_thread_id:
            return await self._render(current_thread_id, status="Pick a saved session first.")
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
        decision: str,
        thread_id: str,
    ) -> AsyncGenerator[tuple[Any, ...], None]:
        text = (user_text or "").strip()
        decision = (decision or "").strip()
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
            payload = make_initial_state(text, int(rounds))
            pending_user = ViewEntry("idea", "You", text)
            first = SPEAKER_ORDER[0]
            running = (first, f"{first} is drafting the next panel turn...")
            idea_title = text
            values = {}
        elif mode == MODE_ANSWERS:
            if not text:
                yield await self._render(thread_id, status="Please answer the questions before submitting.")
                return
            payload = Command(resume=text)
            pending_user = ViewEntry("user_answers", "You", text)
            running = running_from_state(values, ("architect",))
        elif mode == MODE_ARCH_CHOICE:
            option = "B" if decision == ARCH_CHOICE_B else "A"
            payload = Command(resume={"option": option, "notes": text})
            shown = f"Architecture choice: Option {option}" + (f" — {text}" if text else "")
            pending_user = ViewEntry("arch_decision", "You", shown)
            running = running_from_state(values, ("strategy",))
        elif mode == MODE_PLAN_GATE:
            generate = decision == PLAN_CHOICE_GENERATE
            payload = Command(resume={"generate": generate, "notes": text})
            shown = (PLAN_CHOICE_GENERATE if generate else PLAN_CHOICE_SKIP) + (f" — {text}" if text else "")
            pending_user = ViewEntry("planner_decision", "You", shown)
            running = running_from_state(values, ("plan_bundle",)) if generate else None
        elif mode == MODE_IMPL_GATE:
            implement = decision == IMPL_CHOICE_START
            payload = Command(resume={"implement": implement, "notes": text})
            shown = (IMPL_CHOICE_START if implement else IMPL_CHOICE_SKIP) + (f" — {text}" if text else "")
            pending_user = ViewEntry("implement_decision", "You", shown)
            running = running_from_state(values, ("implementer",)) if implement else None
        else:  # MODE_INTERRUPTED: continue from the last checkpoint
            payload = None
            running = running_from_state(values, next_nodes)

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
        config = run_config(thread_id)
        try:
            async for _event in graph.astream(payload, config=config, stream_mode="updates"):
                yield await self._render(thread_id, idea_title=idea_title)
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
        yield await self._render(thread_id, idea_title=idea_title)

    # ----------------------------------------------------------------- export

    async def save_conversation(self, user_text: str, thread_id: str) -> tuple[Any, Any]:
        draft_input = (user_text or "").strip()
        values, interrupt_payload, next_nodes = await self._read(thread_id)
        entries = transcript_from_state(values, interrupt_payload)
        if not entries and not draft_input:
            return gr.update(value="Nothing to save yet."), gr.update(value=None, visible=False)
        mode = mode_from_state(values, interrupt_payload, next_nodes=next_nodes)
        try:
            export_path = save_session_markdown(
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
            gr.update(value=f"Saved conversation to `{export_path}`."),
            gr.update(value=str(export_path), visible=True),
        )

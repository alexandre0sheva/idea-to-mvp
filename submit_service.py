from __future__ import annotations

import logging
import uuid
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import gradio as gr
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

try:
    from .agents import display_speaker_name
    from .config import Settings
    from .exporter import save_session_markdown
    from .render import architect_block, questions_block, summary_block, thinking_block, turn_block
    from .roles import SPEAKER_ORDER
    from .state import IdeaDiscussionState
    from .text_utils import normalize_message_content
except ImportError:
    from agents import display_speaker_name
    from config import Settings
    from exporter import save_session_markdown
    from render import architect_block, questions_block, summary_block, thinking_block, turn_block
    from roles import SPEAKER_ORDER
    from state import IdeaDiscussionState
    from text_utils import normalize_message_content

LOGGER = logging.getLogger(__name__)

MODE_IDEA = "idea"
MODE_ANSWERS = "answers"
MODE_PLAN_GATE = "plan_gate"
MODE_DONE = "done"

PLAN_CHOICE_GENERATE = "Generate the execution pack"
PLAN_CHOICE_SKIP = "Skip for now"

_INPUT_LABELS: dict[str, tuple[str, str]] = {
    MODE_IDEA: ("Describe your idea", "Describe your product idea..."),
    MODE_ANSWERS: ("Your answers to MVP decision questions", "1. ...\n2. ..."),
    MODE_PLAN_GATE: ("Planning notes (optional)", "Anything the planner should account for..."),
    MODE_DONE: ("Describe your next idea", "Describe your product idea..."),
}
_BUTTON_LABELS: dict[str, str] = {
    MODE_IDEA: "Run discussion",
    MODE_ANSWERS: "Submit answers",
    MODE_PLAN_GATE: "Send decision",
    MODE_DONE: "Run discussion",
}


@dataclass(frozen=True)
class AppContext:
    settings: Settings
    graph: CompiledStateGraph


def _initial_state(idea: str, rounds: int) -> IdeaDiscussionState:
    return {
        "user_idea": idea,
        "discussion_history": [HumanMessage(content=idea)],
        "summary": "",
        "generated_questions": [],
        "user_answers": "",
        "architecture": "",
        "plan_offer_question": "",
        "plan_decision": {"generate": False, "notes": ""},
        "project_bundle_dir": "",
        "project_bundle_files": [],
        "project_bundle_summary": "",
        "stage": "discussion",
        "next_speaker": SPEAKER_ORDER[0],
        "max_rounds": rounds * len(SPEAKER_ORDER),
        "turn_count": 0,
    }


def _error_hint(stage: str, exc: Exception) -> str:
    name = type(exc).__name__.lower()
    if "authentication" in name or "permission" in name or "unauthorized" in name:
        return f"{stage} failed due to API authentication/permission. Check provider keys and model access."
    if "ratelimit" in name or "rate_limit" in name:
        return f"{stage} hit a rate limit. Retry shortly or lower request frequency."
    if "timeout" in name:
        return f"{stage} timed out. Retry with fewer rounds or try another model."
    return f"{stage} failed with `{type(exc).__name__}`: {exc}"


class SubmitService:
    """Bridges Gradio submits to one continuously checkpointed graph thread.

    The graph pauses at `interrupt()` gates; the UI mode mirrors the gate the
    graph is paused at and the next submit resumes it with `Command(resume=...)`.
    """

    def __init__(self, context: AppContext):
        self._context = context
        self._project_dir = Path(__file__).resolve().parent

    # ------------------------------------------------------------------ UI

    def _pack(
        self,
        *,
        status: str,
        mode: str,
        thread_id: str,
        turns_state: list[dict[str, str]],
        transcript_state: list[dict[str, str]],
        chat_state: list[dict[str, str]],
        clear_input: bool = False,
    ) -> tuple[Any, ...]:
        label, placeholder = _INPUT_LABELS.get(mode, _INPUT_LABELS[MODE_IDEA])
        input_update = (
            gr.update(value="", label=label, placeholder=placeholder)
            if clear_input
            else gr.update(label=label, placeholder=placeholder)
        )
        return (
            gr.update(value=status),
            chat_state,
            input_update,
            gr.update(visible=mode in (MODE_IDEA, MODE_DONE)),
            gr.update(visible=mode == MODE_PLAN_GATE),
            gr.update(value=_BUTTON_LABELS.get(mode, _BUTTON_LABELS[MODE_IDEA])),
            thread_id,
            mode,
            turns_state,
            transcript_state,
            chat_state,
        )

    def clear_session(self) -> tuple[Any, ...]:
        return self._pack(
            status="",
            mode=MODE_IDEA,
            thread_id=str(uuid.uuid4()),
            turns_state=[],
            transcript_state=[],
            chat_state=[],
            clear_input=True,
        )

    # ------------------------------------------------------- chat helpers

    @staticmethod
    def _append_thinking(
        chat_state: list[dict[str, str]],
        transcript_state: list[dict[str, str]],
        speaker: str,
        message: str,
    ) -> None:
        chat_state.append({"role": "assistant", "content": thinking_block(speaker, message)})
        transcript_state.append({"kind": "thinking", "speaker": speaker, "content": message})

    @staticmethod
    def _replace_or_append(
        chat_state: list[dict[str, str]],
        transcript_state: list[dict[str, str]],
        chat_html: str,
        kind: str,
        speaker: str,
        content: str,
    ) -> None:
        entry = {"kind": kind, "speaker": speaker, "content": content}
        message = {"role": "assistant", "content": chat_html}
        if transcript_state and (transcript_state[-1].get("kind") or "") == "thinking":
            transcript_state[-1] = entry
            chat_state[-1] = message
        else:
            transcript_state.append(entry)
            chat_state.append(message)

    # ------------------------------------------------------ event mapping

    def _apply_discussion(
        self,
        update: dict[str, Any],
        max_turns: int,
        turns_state: list[dict[str, str]],
        transcript_state: list[dict[str, str]],
        chat_state: list[dict[str, str]],
    ) -> str | None:
        history = update.get("discussion_history") or []
        message = history[-1] if history else None
        if not isinstance(message, AIMessage):
            return None
        speaker = display_speaker_name(message.name) if message.name else "Panelist"
        content = normalize_message_content(message)
        self._replace_or_append(
            chat_state, transcript_state, turn_block(speaker, content), "discussion", speaker, content
        )
        turns_state.append({"speaker": speaker, "content": content})
        turn = int(update.get("turn_count", 0))
        if turn < max_turns:
            nxt = update.get("next_speaker", SPEAKER_ORDER[0])
            self._append_thinking(chat_state, transcript_state, nxt, f"{nxt} is drafting the next panel turn...")
            return f"Turn {turn}/{max_turns} complete. Next: {nxt}."
        self._append_thinking(
            chat_state,
            transcript_state,
            "Summarizer",
            "Synthesizing the discussion into a summary and MVP questions...",
        )
        return f"Turn {turn}/{max_turns} complete. Summarizer is preparing the brief."

    def _apply_summary(
        self,
        update: dict[str, Any],
        transcript_state: list[dict[str, str]],
        chat_state: list[dict[str, str]],
    ) -> str:
        summary = (update.get("summary") or "").strip()
        self._replace_or_append(chat_state, transcript_state, summary_block(summary), "summary", "Summary", summary)
        return "Summary ready. Preparing MVP decision questions..."

    def _apply_architect(
        self,
        update: dict[str, Any],
        transcript_state: list[dict[str, str]],
        chat_state: list[dict[str, str]],
    ) -> str:
        architecture = (update.get("architecture") or "").strip() or (
            "Architect returned an empty architecture proposal."
        )
        self._replace_or_append(
            chat_state, transcript_state, architect_block(architecture), "architect", "Architect", architecture
        )
        self._append_thinking(chat_state, transcript_state, "Planner", "Preparing the execution-pack question...")
        return "Architecture ready. Planner is preparing the next step..."

    def _apply_plan_bundle(
        self,
        update: dict[str, Any],
        transcript_state: list[dict[str, str]],
        chat_state: list[dict[str, str]],
    ) -> str:
        summary = (update.get("project_bundle_summary") or "").strip()
        summary = summary or (update.get("project_bundle_dir") or "").strip()
        summary = summary or "Project pack generation returned no visible summary."
        self._replace_or_append(
            chat_state, transcript_state, turn_block("Planner", summary), "project_bundle", "Planner", summary
        )
        return "Project pack ready."

    def _apply_interrupt(
        self,
        interrupts: Any,
        transcript_state: list[dict[str, str]],
        chat_state: list[dict[str, str]],
    ) -> tuple[str, str]:
        value = interrupts[0].value if interrupts else None
        payload = value if isinstance(value, dict) else {}
        kind = payload.get("kind")
        if kind == "answers":
            questions = [str(q) for q in payload.get("questions") or []]
            block = questions_block(questions)
            if block:
                self._replace_or_append(
                    chat_state, transcript_state, block, "questions", "Questions", "\n".join(questions)
                )
            return MODE_ANSWERS, "Answer the MVP decision questions to continue."
        if kind == "plan_gate":
            question = str(payload.get("question") or "").strip() or "Generate the execution pack?"
            self._replace_or_append(
                chat_state, transcript_state, turn_block("Planner", question), "planner_offer", "Planner", question
            )
            return MODE_PLAN_GATE, "Choose whether to generate the execution pack. Notes are optional."
        return MODE_DONE, "Pipeline paused at an unknown gate. Use Clear to restart."

    # ------------------------------------------------------------ running

    def _run(
        self,
        *,
        payload: Any,
        max_turns: int,
        thread_id: str,
        mode: str,
        turns_state: list[dict[str, str]],
        transcript_state: list[dict[str, str]],
        chat_state: list[dict[str, str]],
    ) -> Generator[tuple[Any, ...], None, None]:
        config = {"configurable": {"thread_id": thread_id}}
        next_mode = MODE_DONE
        final_status = "Pipeline finished. Use Clear to start a fresh session, or describe a new idea."
        try:
            for event in self._context.graph.stream(payload, config=config, stream_mode="updates"):
                if "__interrupt__" in event:
                    next_mode, final_status = self._apply_interrupt(
                        event["__interrupt__"], transcript_state, chat_state
                    )
                    continue
                status: str | None = None
                if "discussion" in event:
                    status = self._apply_discussion(
                        event["discussion"], max_turns, turns_state, transcript_state, chat_state
                    )
                elif "summarizer" in event:
                    status = self._apply_summary(event["summarizer"], transcript_state, chat_state)
                elif "architect" in event:
                    status = self._apply_architect(event["architect"], transcript_state, chat_state)
                elif "plan_bundle" in event:
                    status = self._apply_plan_bundle(event["plan_bundle"], transcript_state, chat_state)
                # collect_answers / plan_gate / planner_offer updates need no chat output:
                # their visible content arrives via the interrupt payloads.
                if status:
                    yield self._pack(
                        status=status,
                        mode=mode,
                        thread_id=thread_id,
                        turns_state=turns_state,
                        transcript_state=transcript_state,
                        chat_state=chat_state,
                    )
        except Exception as exc:
            LOGGER.exception("Pipeline run failed")
            hint = _error_hint("Pipeline", exc)
            chat_state.append({"role": "assistant", "content": f"### Orchestrator error\n\n{hint}"})
            transcript_state.append({"kind": "system", "speaker": "Orchestrator error", "content": hint})
            next_mode = mode if mode in (MODE_ANSWERS, MODE_PLAN_GATE) else MODE_IDEA
            final_status = "Run failed. See the error in chat; you can retry your last input."
        yield self._pack(
            status=final_status,
            mode=next_mode,
            thread_id=thread_id,
            turns_state=turns_state,
            transcript_state=transcript_state,
            chat_state=chat_state,
        )

    # ------------------------------------------------------------- submit

    def handle_submit(
        self,
        user_text: str,
        rounds: int,
        plan_choice: str,
        thread_id: str,
        mode: str,
        turns_state: list[dict[str, str]] | None = None,
        transcript_state: list[dict[str, str]] | None = None,
        chat_state: list[dict[str, str]] | None = None,
    ) -> Generator[tuple[Any, ...], None, None]:
        text = (user_text or "").strip()
        thread_id = (thread_id or "").strip() or str(uuid.uuid4())
        mode = (mode or MODE_IDEA).strip() or MODE_IDEA
        turns_state = list(turns_state or [])
        transcript_state = list(transcript_state or [])
        chat_state = list(chat_state or [])

        if mode in (MODE_IDEA, MODE_DONE):
            if not text:
                yield self._pack(
                    status="Please describe your idea first.",
                    mode=MODE_IDEA,
                    thread_id=thread_id,
                    turns_state=turns_state,
                    transcript_state=transcript_state,
                    chat_state=chat_state,
                )
                return
            if mode == MODE_DONE:
                thread_id = str(uuid.uuid4())
            turns_state, transcript_state, chat_state = [], [], []
            state = _initial_state(text, int(rounds))
            chat_state.append({"role": "user", "content": text})
            transcript_state.append({"kind": "idea", "speaker": "You", "content": text})
            self._append_thinking(
                chat_state,
                transcript_state,
                state["next_speaker"],
                f"{state['next_speaker']} is drafting the next panel turn...",
            )
            yield self._pack(
                status="Running panel discussion...",
                mode=MODE_IDEA,
                thread_id=thread_id,
                turns_state=turns_state,
                transcript_state=transcript_state,
                chat_state=chat_state,
                clear_input=True,
            )
            yield from self._run(
                payload=state,
                max_turns=state["max_rounds"],
                thread_id=thread_id,
                mode=MODE_IDEA,
                turns_state=turns_state,
                transcript_state=transcript_state,
                chat_state=chat_state,
            )
            return

        if mode == MODE_ANSWERS:
            if not text:
                yield self._pack(
                    status="Please answer the questions before submitting.",
                    mode=mode,
                    thread_id=thread_id,
                    turns_state=turns_state,
                    transcript_state=transcript_state,
                    chat_state=chat_state,
                )
                return
            chat_state.append({"role": "user", "content": text})
            transcript_state.append({"kind": "user_answers", "speaker": "You", "content": text})
            self._append_thinking(
                chat_state, transcript_state, "Architect", "Turning your answers into two architecture options..."
            )
            yield self._pack(
                status="Answers captured. Architect is designing system options...",
                mode=mode,
                thread_id=thread_id,
                turns_state=turns_state,
                transcript_state=transcript_state,
                chat_state=chat_state,
                clear_input=True,
            )
            yield from self._run(
                payload=Command(resume=text),
                max_turns=0,
                thread_id=thread_id,
                mode=mode,
                turns_state=turns_state,
                transcript_state=transcript_state,
                chat_state=chat_state,
            )
            return

        if mode == MODE_PLAN_GATE:
            generate = (plan_choice or "").strip() == PLAN_CHOICE_GENERATE
            decision_label = PLAN_CHOICE_GENERATE if generate else PLAN_CHOICE_SKIP
            shown = decision_label + (f" — {text}" if text else "")
            chat_state.append({"role": "user", "content": shown})
            transcript_state.append({"kind": "planner_decision", "speaker": "You", "content": shown})
            if generate:
                self._append_thinking(
                    chat_state,
                    transcript_state,
                    "Planner",
                    "Generating `AGENTS.md` guidance files and a task-only `plan.md`...",
                )
                yield self._pack(
                    status="Generating the agent-ready project pack...",
                    mode=mode,
                    thread_id=thread_id,
                    turns_state=turns_state,
                    transcript_state=transcript_state,
                    chat_state=chat_state,
                    clear_input=True,
                )
            yield from self._run(
                payload=Command(resume={"generate": generate, "notes": text}),
                max_turns=0,
                thread_id=thread_id,
                mode=mode,
                turns_state=turns_state,
                transcript_state=transcript_state,
                chat_state=chat_state,
            )
            return

        yield self._pack(
            status=f"Unknown mode `{mode}`. Use Clear to reset the session.",
            mode=MODE_IDEA,
            thread_id=thread_id,
            turns_state=turns_state,
            transcript_state=transcript_state,
            chat_state=chat_state,
        )

    # -------------------------------------------------------------- export

    def save_conversation(
        self,
        user_text: str,
        thread_id: str,
        mode: str,
        transcript_state: list[dict[str, str]],
    ) -> tuple[Any, Any]:
        draft_input = (user_text or "").strip()
        transcript_state = transcript_state or []
        if not transcript_state and not draft_input:
            return gr.update(value="Nothing to save yet."), gr.update(value=None, visible=False)

        try:
            export_path = save_session_markdown(
                project_dir=self._project_dir,
                transcript_state=transcript_state,
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

from __future__ import annotations

import logging
import uuid
from collections.abc import Generator
from dataclasses import dataclass
from typing import Any

import gradio as gr
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph.state import CompiledStateGraph

try:
    from .agents import SPEAKER_ORDER, display_speaker_name
    from .config import Settings
    from .render import architect_block, questions_block, summary_block, thinking_block, turn_block
    from .state import IdeaDiscussionState
    from .text_utils import normalize_message_content
except ImportError:
    from agents import SPEAKER_ORDER, display_speaker_name
    from config import Settings
    from render import architect_block, questions_block, summary_block, thinking_block, turn_block
    from state import IdeaDiscussionState
    from text_utils import normalize_message_content

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class AppContext:
    settings: Settings
    graph: CompiledStateGraph


def _pack(
    status: str,
    chat_state: list[dict[str, str]],
    turns_state: list[dict[str, str]],
    input_box: Any,
    rounds_box: Any,
    run_btn: Any,
    thread_id: str,
    mode: str,
    questions_state: list[str],
    summary_state: str,
) -> tuple[Any, ...]:
    return (
        gr.update(value=status),
        chat_state,
        input_box,
        rounds_box,
        run_btn,
        thread_id,
        mode,
        questions_state,
        summary_state,
        turns_state,
        chat_state,
    )


def _initial_state(idea: str, rounds: int) -> IdeaDiscussionState:
    return {
        "user_idea": idea,
        "discussion_history": [HumanMessage(content=idea)],
        "summary": "",
        "generated_questions": [],
        "user_answers": "",
        "architecture": "",
        "phase": "idea",
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
    def __init__(self, context: AppContext):
        self._context = context

    def clear_session(self) -> tuple[Any, ...]:
        return (
            gr.update(value=""),
            [],
            gr.update(value="", label="Describe your idea", placeholder="Describe your product idea..."),
            gr.update(visible=True, value=self._context.settings.default_rounds),
            gr.update(value="Run discussion"),
            str(uuid.uuid4()),
            "idea",
            [],
            "",
            [],
            [],
        )

    def handle_submit(
        self,
        user_text: str,
        rounds: int,
        thread_id: str,
        mode: str,
        questions_state: list[str],
        summary_state: str,
        turns_state: list[dict[str, str]],
        chat_state: list[dict[str, str]],
    ) -> Generator[tuple[Any, ...], None, None]:
        text = (user_text or "").strip()
        thread_id = (thread_id or "").strip() or str(uuid.uuid4())
        mode = mode or "idea"
        questions_state = questions_state or []
        summary_state = summary_state or ""
        turns_state = turns_state or []
        chat_state = chat_state or []

        if not text:
            yield _pack(
                "Please enter text before submitting.",
                chat_state,
                turns_state,
                gr.update(),
                gr.update(),
                gr.update(),
                thread_id,
                mode,
                questions_state,
                summary_state,
            )
            return

        if mode == "answers":
            yield from self._run_architect_mode(
                text=text,
                thread_id=thread_id,
                questions_state=questions_state,
                summary_state=summary_state,
                turns_state=turns_state,
                chat_state=chat_state,
            )
            return

        yield from self._run_idea_mode(
            text=text,
            rounds=rounds,
            thread_id=thread_id,
            turns_state=turns_state,
            chat_state=chat_state,
        )

    def _run_architect_mode(
        self,
        *,
        text: str,
        thread_id: str,
        questions_state: list[str],
        summary_state: str,
        turns_state: list[dict[str, str]],
        chat_state: list[dict[str, str]],
    ) -> Generator[tuple[Any, ...], None, None]:
        chat_state += [
            {"role": "user", "content": text},
            {"role": "assistant", "content": thinking_block("Architect")},
        ]
        yield _pack(
            "Answers captured. Architect is designing system options...",
            chat_state,
            turns_state,
            gr.update(value="", label="Add clarifications (optional)"),
            gr.update(visible=False),
            gr.update(value="Update answers"),
            thread_id,
            "answers",
            questions_state,
            summary_state,
        )

        initial_idea = ""
        for item in chat_state:
            if item.get("role") == "user":
                initial_idea = (item.get("content") or "").strip()
                if initial_idea:
                    break
        try:
            architect_state: IdeaDiscussionState = {
                "user_idea": initial_idea or text,
                "discussion_history": [HumanMessage(content=initial_idea or text)],
                "summary": summary_state,
                "generated_questions": questions_state,
                "user_answers": text,
                "architecture": "",
                "phase": "answers",
                "next_speaker": SPEAKER_ORDER[0],
                "max_rounds": 0,
                "turn_count": 0,
            }
            architecture_text = ""
            for event in self._context.graph.stream(
                architect_state,
                config={"configurable": {"thread_id": thread_id}},
                stream_mode="updates",
            ):
                if "architect" in event:
                    architecture_text = (event["architect"].get("architecture") or "").strip()
            if not architecture_text:
                architecture_text = "Architect returned an empty architecture proposal."
        except Exception as exc:
            LOGGER.exception("Architect stage failed")
            architecture_text = _error_hint("Architect stage", exc)
        chat_state[-1] = {"role": "assistant", "content": architect_block(architecture_text)}
        yield _pack(
            "Architecture options ready. You can refine answers and re-run.",
            chat_state,
            turns_state,
            gr.update(value="", label="Add clarifications (optional)"),
            gr.update(visible=False),
            gr.update(value="Update answers"),
            thread_id,
            "answers",
            questions_state,
            summary_state,
        )

    def _run_idea_mode(
        self,
        *,
        text: str,
        rounds: int,
        thread_id: str,
        turns_state: list[dict[str, str]],
        chat_state: list[dict[str, str]],
    ) -> Generator[tuple[Any, ...], None, None]:
        state = _initial_state(text, int(rounds))
        max_turns = state["max_rounds"]
        turns_state = []
        chat_state = [
            {"role": "user", "content": text},
            {"role": "assistant", "content": thinking_block(state["next_speaker"])},
        ]
        yield _pack(
            "Running panel discussion...",
            chat_state,
            turns_state,
            gr.update(value="", label="Your answers to MVP decision questions"),
            gr.update(visible=False),
            gr.update(value="Submit answers"),
            thread_id,
            "idea",
            [],
            "",
        )
        try:
            for event in self._context.graph.stream(
                state,
                config={"configurable": {"thread_id": thread_id}},
                stream_mode="updates",
            ):
                if "discussion" in event:
                    d = event["discussion"]
                    msg = (d.get("discussion_history") or [None])[-1]
                    speaker = display_speaker_name(msg.name) if isinstance(msg, AIMessage) and msg.name else "Panelist"
                    content = normalize_message_content(msg)
                    chat_state[-1] = {"role": "assistant", "content": turn_block(speaker, content)}
                    turns_state.append({"speaker": speaker, "content": content})
                    turn = int(d.get("turn_count", 0))
                    if turn < max_turns:
                        nxt = d.get("next_speaker", SPEAKER_ORDER[0])
                        chat_state.append({"role": "assistant", "content": thinking_block(nxt)})
                        yield _pack(
                            f"Turn {turn}/{max_turns} complete. Next: {nxt}.",
                            chat_state,
                            turns_state,
                            gr.update(),
                            gr.update(visible=False),
                            gr.update(value="Submit answers"),
                            thread_id,
                            "idea",
                            [],
                            "",
                        )
                    else:
                        chat_state.append({"role": "assistant", "content": thinking_block("Summarizer")})
                        yield _pack(
                            "Discussion complete. Summarizer is preparing MVP questions.",
                            chat_state,
                            turns_state,
                            gr.update(),
                            gr.update(visible=False),
                            gr.update(value="Submit answers"),
                            thread_id,
                            "idea",
                            [],
                            "",
                        )

                if "summarizer" in event:
                    s = event["summarizer"]
                    summary = (s.get("summary") or "").strip()
                    questions = (s.get("generated_questions") or [])[:5]
                    q_block = questions_block(questions)
                    chat_state[-1] = {"role": "assistant", "content": summary_block(summary)}
                    if q_block:
                        chat_state.append({"role": "assistant", "content": q_block})
                    yield _pack(
                        "Done. Answer MVP decision questions to continue.",
                        chat_state,
                        turns_state,
                        gr.update(value="", label="Your answers to MVP decision questions", placeholder="1. ...\n2. ..."),
                        gr.update(visible=False),
                        gr.update(value="Submit answers"),
                        thread_id,
                        "answers",
                        questions,
                        summary,
                    )
        except Exception as exc:
            LOGGER.exception("Orchestrator run failed")
            chat_state.append({"role": "assistant", "content": f"### Orchestrator error\n\n{_error_hint('Orchestrator run', exc)}"})
            yield _pack(
                "Run failed. See error in chat.",
                chat_state,
                turns_state,
                gr.update(),
                gr.update(visible=False),
                gr.update(value="Run discussion"),
                thread_id,
                "idea",
                [],
                "",
            )

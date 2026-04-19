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

try:
    from .agents import SPEAKER_ORDER, display_speaker_name
    from .config import Settings
    from .exporter import save_session_markdown
    from .render import architect_block, questions_block, summary_block, thinking_block, turn_block
    from .state import IdeaDiscussionState
    from .text_utils import normalize_message_content
except ImportError:
    from agents import SPEAKER_ORDER, display_speaker_name
    from config import Settings
    from exporter import save_session_markdown
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
    transcript_state: list[dict[str, str]],
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
        transcript_state,
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
        "plan_offer_question": "",
        "planning_request": "",
        "project_bundle_dir": "",
        "project_bundle_files": [],
        "project_bundle_summary": "",
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


def _latest_transcript_content(transcript_state: list[dict[str, str]], kind: str) -> str:
    for entry in reversed(transcript_state or []):
        if (entry.get("kind") or "").strip() == kind:
            return (entry.get("content") or "").strip()
    return ""


def _is_affirmative(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    yes_prefixes = (
        "y",
        "yes",
        "sure",
        "ok",
        "okay",
        "please do",
        "do it",
        "generate",
        "create",
        "go ahead",
        "proceed",
    )
    return any(normalized.startswith(prefix) for prefix in yes_prefixes)


def _is_negative(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    no_prefixes = ("n", "no", "not now", "skip", "later", "nope")
    return any(normalized.startswith(prefix) for prefix in no_prefixes)


class SubmitService:
    def __init__(self, context: AppContext):
        self._context = context
        self._project_dir = Path(__file__).resolve().parent

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
        transcript_state: list[dict[str, str]],
        chat_state: list[dict[str, str]],
    ) -> Generator[tuple[Any, ...], None, None]:
        text = (user_text or "").strip()
        thread_id = (thread_id or "").strip() or str(uuid.uuid4())
        mode = mode or "idea"
        questions_state = questions_state or []
        summary_state = summary_state or ""
        turns_state = turns_state or []
        transcript_state = transcript_state or []
        chat_state = chat_state or []

        if not text:
            yield _pack(
                "Please enter text before submitting.",
                chat_state,
                turns_state,
                transcript_state,
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
                transcript_state=transcript_state,
                chat_state=chat_state,
            )
            return

        if mode == "plan_offer":
            if _is_affirmative(text):
                yield from self._run_plan_bundle_mode(
                    text=text,
                    thread_id=thread_id,
                    questions_state=questions_state,
                    summary_state=summary_state,
                    turns_state=turns_state,
                    transcript_state=transcript_state,
                    chat_state=chat_state,
                )
                return

            if _is_negative(text):
                chat_state += [
                    {"role": "user", "content": text},
                    {
                        "role": "assistant",
                        "content": turn_block(
                            "Planner",
                            "No problem. If you want the agent-ready project pack later, reply with `yes` and I will generate it.",
                        ),
                    },
                ]
                transcript_state += [
                    {"kind": "planner_decision", "speaker": "You", "content": text},
                    {
                        "kind": "planner_note",
                        "speaker": "Planner",
                        "content": "User declined project-pack generation for now.",
                    },
                ]
                yield _pack(
                    "Skipped project-pack generation. You can still answer `yes` later.",
                    chat_state,
                    turns_state,
                    transcript_state,
                    gr.update(value="", label="Reply `yes` later to generate the project pack"),
                    gr.update(visible=False),
                    gr.update(value="Send"),
                    thread_id,
                    "plan_offer",
                    questions_state,
                    summary_state,
                )
                return

            yield _pack(
                "Please answer with `yes` to generate the project pack or `no` to skip it for now.",
                chat_state,
                turns_state,
                transcript_state,
                gr.update(value=text, label="Do you want the execution pack?", placeholder="yes / no"),
                gr.update(visible=False),
                gr.update(value="Send"),
                thread_id,
                mode,
                questions_state,
                summary_state,
            )
            return

        yield from self._run_idea_mode(
            text=text,
            rounds=rounds,
            thread_id=thread_id,
            turns_state=turns_state,
            transcript_state=transcript_state,
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
        transcript_state: list[dict[str, str]],
        chat_state: list[dict[str, str]],
    ) -> Generator[tuple[Any, ...], None, None]:
        chat_state += [
            {"role": "user", "content": text},
            {
                "role": "assistant",
                "content": thinking_block("Architect", "Turning your answers into two concise architecture options..."),
            },
        ]
        transcript_state += [
            {"kind": "user_answers", "speaker": "You", "content": text},
            {
                "kind": "thinking",
                "speaker": "Architect",
                "content": "Designing system options...",
            },
        ]
        yield _pack(
            "Answers captured. Architect is designing system options...",
            chat_state,
            turns_state,
            transcript_state,
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
                "plan_offer_question": "",
                "planning_request": "",
                "project_bundle_dir": "",
                "project_bundle_files": [],
                "project_bundle_summary": "",
                "phase": "answers",
                "next_speaker": SPEAKER_ORDER[0],
                "max_rounds": 0,
                "turn_count": 0,
            }
            architecture_text = ""
            planner_question = ""
            for event in self._context.graph.stream(
                architect_state,
                config={"configurable": {"thread_id": thread_id}},
                stream_mode="updates",
            ):
                if "architect" in event:
                    architecture_text = (event["architect"].get("architecture") or "").strip()
                    chat_state[-1] = {"role": "assistant", "content": architect_block(architecture_text or "Architect returned an empty architecture proposal.")}
                    transcript_state[-1] = {
                        "kind": "architect",
                        "speaker": "Architect",
                        "content": architecture_text or "Architect returned an empty architecture proposal.",
                    }
                    chat_state.append(
                        {
                            "role": "assistant",
                            "content": thinking_block("Planner", "Preparing the next-step question about generating the execution pack..."),
                        }
                    )
                    transcript_state.append(
                        {
                            "kind": "thinking",
                            "speaker": "Planner",
                            "content": "Preparing the next-step delivery question...",
                        }
                    )
                    yield _pack(
                        "Architecture ready. Preparing the next implementation step...",
                        chat_state,
                        turns_state,
                        transcript_state,
                        gr.update(value="", label="Add clarifications (optional)"),
                        gr.update(visible=False),
                        gr.update(value="Update answers"),
                        thread_id,
                        "answers",
                        questions_state,
                        summary_state,
                    )
                if "planner_offer" in event:
                    planner_question = (event["planner_offer"].get("plan_offer_question") or "").strip()
            if not architecture_text:
                architecture_text = "Architect returned an empty architecture proposal."
            if not planner_question:
                planner_question = (
                    "Should I generate an agent-ready project pack next with scoped `AGENTS.md` files and a full `plan.md` "
                    "covering task order, data contracts, and test expectations for the MVP?"
                )
        except Exception as exc:
            LOGGER.exception("Architect stage failed")
            architecture_text = _error_hint("Architect stage", exc)
            planner_question = ""
        if not transcript_state or (transcript_state[-1].get("kind") or "") != "planner_offer":
            if not transcript_state or (transcript_state[-1].get("kind") or "") != "thinking":
                chat_state.append({"role": "assistant", "content": architect_block(architecture_text)})
                transcript_state.append(
                    {
                        "kind": "architect",
                        "speaker": "Architect",
                        "content": architecture_text,
                    }
                )
            elif (transcript_state[-1].get("speaker") or "") == "Architect":
                chat_state[-1] = {"role": "assistant", "content": architect_block(architecture_text)}
                transcript_state[-1] = {
                    "kind": "architect",
                    "speaker": "Architect",
                    "content": architecture_text,
                }

        if planner_question:
            if chat_state and transcript_state and (transcript_state[-1].get("kind") or "") == "thinking":
                chat_state[-1] = {"role": "assistant", "content": turn_block("Planner", planner_question)}
                transcript_state[-1] = {
                    "kind": "planner_offer",
                    "speaker": "Planner",
                    "content": planner_question,
                }
            else:
                chat_state.append({"role": "assistant", "content": turn_block("Planner", planner_question)})
                transcript_state.append(
                    {
                        "kind": "planner_offer",
                        "speaker": "Planner",
                        "content": planner_question,
                    }
                )
        yield _pack(
            "Architecture options ready. Decide whether to generate the execution pack.",
            chat_state,
            turns_state,
            transcript_state,
            gr.update(value="", label=planner_question or "Generate the execution pack?", placeholder="yes / no"),
            gr.update(visible=False),
            gr.update(value="Send"),
            thread_id,
            "plan_offer",
            questions_state,
            summary_state,
        )

    def _run_plan_bundle_mode(
        self,
        *,
        text: str,
        thread_id: str,
        questions_state: list[str],
        summary_state: str,
        turns_state: list[dict[str, str]],
        transcript_state: list[dict[str, str]],
        chat_state: list[dict[str, str]],
    ) -> Generator[tuple[Any, ...], None, None]:
        architecture_text = _latest_transcript_content(transcript_state, "architect")
        initial_idea = ""
        for item in chat_state:
            if item.get("role") == "user":
                initial_idea = (item.get("content") or "").strip()
                if initial_idea:
                    break

        chat_state += [
            {"role": "user", "content": text},
            {
                "role": "assistant",
                "content": thinking_block("Planner", "Generating `AGENTS.md` guidance files and a task-only `plan.md`..."),
            },
        ]
        transcript_state += [
            {"kind": "planner_decision", "speaker": "You", "content": text},
            {
                "kind": "thinking",
                "speaker": "Planner",
                "content": "Generating the project pack and execution plan...",
            },
        ]
        yield _pack(
            "Generating the agent-ready project pack...",
            chat_state,
            turns_state,
            transcript_state,
            gr.update(value="", label="Add planning notes (optional)"),
            gr.update(visible=False),
            gr.update(value="Generate again"),
            thread_id,
            "plan_offer",
            questions_state,
            summary_state,
        )

        try:
            plan_state: IdeaDiscussionState = {
                "user_idea": initial_idea or text,
                "discussion_history": [HumanMessage(content=initial_idea or text)],
                "summary": summary_state,
                "generated_questions": questions_state,
                "user_answers": _latest_transcript_content(transcript_state, "user_answers"),
                "architecture": architecture_text,
                "plan_offer_question": _latest_transcript_content(transcript_state, "planner_offer"),
                "planning_request": text,
                "project_bundle_dir": "",
                "project_bundle_files": [],
                "project_bundle_summary": "",
                "phase": "plan_bundle",
                "next_speaker": SPEAKER_ORDER[0],
                "max_rounds": 0,
                "turn_count": 0,
            }
            plan_summary = ""
            bundle_dir = ""
            for event in self._context.graph.stream(
                plan_state,
                config={"configurable": {"thread_id": f"{thread_id}-plan"}},
                stream_mode="updates",
            ):
                if "plan_bundle" in event:
                    bundle_event = event["plan_bundle"]
                    plan_summary = (bundle_event.get("project_bundle_summary") or "").strip()
                    bundle_dir = (bundle_event.get("project_bundle_dir") or "").strip()
            if not plan_summary:
                plan_summary = bundle_dir or "Project pack generation returned no visible summary."
        except Exception as exc:
            LOGGER.exception("Project pack generation failed")
            plan_summary = _error_hint("Project pack generation", exc)

        chat_state[-1] = {"role": "assistant", "content": turn_block("Planner", plan_summary)}
        transcript_state[-1] = {
            "kind": "project_bundle",
            "speaker": "Planner",
            "content": plan_summary,
        }
        yield _pack(
            "Project pack ready.",
            chat_state,
            turns_state,
            transcript_state,
            gr.update(
                value="",
                label="Reply `yes` to generate another version, or add extra planning notes and submit again",
            ),
            gr.update(visible=False),
            gr.update(value="Send"),
            thread_id,
            "plan_offer",
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
        transcript_state: list[dict[str, str]],
        chat_state: list[dict[str, str]],
    ) -> Generator[tuple[Any, ...], None, None]:
        state = _initial_state(text, int(rounds))
        max_turns = state["max_rounds"]
        turns_state = []
        chat_state = [
            {"role": "user", "content": text},
            {
                "role": "assistant",
                "content": thinking_block(state["next_speaker"], f"{state['next_speaker']} is drafting the next panel turn..."),
            },
        ]
        transcript_state = [
            {"kind": "idea", "speaker": "You", "content": text},
            {
                "kind": "thinking",
                "speaker": state["next_speaker"],
                "content": f"{state['next_speaker']} is drafting the next panel turn...",
            },
        ]
        yield _pack(
            "Running panel discussion...",
            chat_state,
            turns_state,
            transcript_state,
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
                    transcript_state[-1] = {
                        "kind": "discussion",
                        "speaker": speaker,
                        "content": content,
                    }
                    turn = int(d.get("turn_count", 0))
                    if turn < max_turns:
                        nxt = d.get("next_speaker", SPEAKER_ORDER[0])
                        chat_state.append(
                            {
                                "role": "assistant",
                                "content": thinking_block(nxt, f"{nxt} is drafting the next panel turn..."),
                            }
                        )
                        transcript_state.append(
                            {
                                "kind": "thinking",
                                "speaker": nxt,
                                "content": f"{nxt} is drafting the next panel turn...",
                            }
                        )
                        yield _pack(
                            f"Turn {turn}/{max_turns} complete. Next: {nxt}.",
                            chat_state,
                            turns_state,
                            transcript_state,
                            gr.update(),
                            gr.update(visible=False),
                            gr.update(value="Submit answers"),
                            thread_id,
                            "idea",
                            [],
                            "",
                        )
                    else:
                        chat_state.append(
                            {
                                "role": "assistant",
                                "content": thinking_block("Summarizer", "Synthesizing the discussion into a summary and MVP questions..."),
                            }
                        )
                        transcript_state.append(
                            {
                                "kind": "thinking",
                                "speaker": "Summarizer",
                                "content": "Preparing summary and MVP questions...",
                            }
                        )
                        yield _pack(
                            "Discussion complete. Summarizer is preparing MVP questions.",
                            chat_state,
                            turns_state,
                            transcript_state,
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
                    transcript_state[-1] = {
                        "kind": "summary",
                        "speaker": "Summary",
                        "content": summary,
                    }
                    if q_block:
                        chat_state.append({"role": "assistant", "content": q_block})
                        transcript_state.append(
                            {
                                "kind": "questions",
                                "speaker": "Questions",
                                "content": "\n".join(questions),
                            }
                        )
                    yield _pack(
                        "Done. Answer MVP decision questions to continue.",
                        chat_state,
                        turns_state,
                        transcript_state,
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
            error_text = _error_hint("Orchestrator run", exc)
            chat_state.append({"role": "assistant", "content": f"### Orchestrator error\n\n{error_text}"})
            transcript_state.append(
                {
                    "kind": "system",
                    "speaker": "Orchestrator error",
                    "content": error_text,
                }
            )
            yield _pack(
                "Run failed. See error in chat.",
                chat_state,
                turns_state,
                transcript_state,
                gr.update(),
                gr.update(visible=False),
                gr.update(value="Run discussion"),
                thread_id,
                "idea",
                [],
                "",
            )

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

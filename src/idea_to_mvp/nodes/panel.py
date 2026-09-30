"""The panel as a subgraph: parallel opening statements, then a moderator-driven debate.

```
moderated:   START ─Send×3→ opening_turn ─→ merge_openings ─→ moderator ⇄ speaker_turn ─→ END
round_robin: START ─→ speaker_turn ⟲ (fixed PM → Tech Lead → Skeptic rotation) ─→ END
```

The opening statements are independent, so the three speakers run concurrently and `merge_openings`
puts them into the transcript in canonical order whatever finished first. After that the moderator (a
cheap structured call) picks who speaks next and may end the debate early. Code, not the prompt, enforces
the moderator's hard rules: nobody converges before every speaker has had two turns, nobody speaks twice
in a row, and the turn budget (`max_rounds`, already rounds x speakers) is a hard cap.
"""

from __future__ import annotations

from collections import Counter
from typing import Annotated, Any, Final, Literal, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Send

from idea_to_mvp import llm
from idea_to_mvp.config import PanelMode
from idea_to_mvp.nodes.common import (
    display_speaker_name,
    history_markdown,
    preferences_block,
    research_block,
)
from idea_to_mvp.nodes.discussion import (
    EMPTY_TURN_TEXT,
    discussion_node,
    round_rules,
    speaker_message,
)
from idea_to_mvp.resilience import LLM_RETRY
from idea_to_mvp.roles import SPEAKER_NAME_TOKEN, SPEAKER_ORDER
from idea_to_mvp.schemas import ModeratorDecision
from idea_to_mvp.state import IdeaDiscussionState, SpeakerName, Stage
from idea_to_mvp.usage import with_usage

_END: Final = "__end__"  # langgraph.graph.END, typed as the literal the routers return
MIN_TURNS_BEFORE_CONVERGENCE = 2
_NO_DECISION_REASON = "The moderator could not decide; the panel continues in rotation."


class PanelInput(TypedDict):
    """What the parent hands the panel. Deliberately leaves out `usage` and `opening_turns`: the
    subgraph starts those channels empty and returns only what it added, so the parent's accumulating
    `usage` reducer does not count the records it already holds a second time."""

    user_idea: str
    preferences: dict[str, Any]
    research: dict[str, Any]
    discussion_history: Annotated[list[BaseMessage], add_messages]
    max_rounds: int
    turn_count: int
    next_speaker: SpeakerName
    panel_mode: PanelMode
    stage: Stage


class OpeningInput(TypedDict):
    """Payload of one `Send("opening_turn", ...)`: a speaker's opening sees only the idea."""

    speaker: str
    user_idea: str
    preferences: dict[str, Any]
    research: dict[str, Any]
    max_rounds: int


# ------------------------------------------------------------------ openings


@with_usage(role=lambda state: SPEAKER_NAME_TOKEN[state["speaker"]])
def opening_turn_node(state: OpeningInput) -> dict[str, Any]:
    speaker = state["speaker"]
    runtime = llm.get_runtime(SPEAKER_NAME_TOKEN[speaker])
    prompt = (
        f"Anchor idea:\n{state['user_idea']}\n\n"
        f"{preferences_block(state.get('preferences'))}"
        f"{research_block(state.get('research'))}"
        f"You are **{speaker}**. Write your OPENING statement for the panel.\n"
        "Rules for this turn:\n"
        f"{round_rules(state['max_rounds'], 0)}"
        "- The other two panelists are writing their opening statements at the same time, so you cannot "
        "see them. This overrides the general rule about referencing other panelists: do not refer to "
        "or react to them yet; state your own position.\n"
        "- Lead with your angle on the idea: the decisions, assumptions, and tradeoffs that matter most.\n"
        "- Keep it concise, structured, and decision-oriented.\n"
        "- Use at most 2 short sections and at most 6 bullets total.\n"
    )
    text = llm.invoke_text(
        runtime,
        [
            SystemMessage(content=runtime.system_prompt),
            HumanMessage(content=prompt + "\n\nReturn the final answer text explicitly."),
        ],
    )
    return {"opening_turns": {speaker: text or EMPTY_TURN_TEXT}}


def merge_openings_node(state: IdeaDiscussionState) -> dict[str, Any]:
    """Append the openings in canonical order (PM, Tech Lead, Skeptic), whichever finished first."""
    openings = state.get("opening_turns") or {}
    messages = [speaker_message(speaker, openings.get(speaker, "")) for speaker in SPEAKER_ORDER]
    return {
        "discussion_history": messages,
        "turn_count": state["turn_count"] + len(messages),
        "next_speaker": SPEAKER_ORDER[0],
        "stage": "discussion",
    }


# ----------------------------------------------------------------- moderator


def _turn_counts(history: list[BaseMessage]) -> Counter[str]:
    return Counter(
        display_speaker_name(message.name)
        for message in history
        if isinstance(message, AIMessage) and display_speaker_name(message.name) in SPEAKER_ORDER
    )


def _last_speaker(history: list[BaseMessage]) -> str | None:
    for message in reversed(history):
        if isinstance(message, AIMessage) and display_speaker_name(message.name) in SPEAKER_ORDER:
            return display_speaker_name(message.name)
    return None


def _least_heard(turns: Counter[str], last: str | None) -> str:
    """Whoever has spoken least (never `last`); ties go to the next speaker in canonical order."""
    candidates = [name for name in SPEAKER_ORDER if name != last]
    fewest = min(turns[name] for name in candidates)
    start = SPEAKER_ORDER.index(last) + 1 if last in SPEAKER_ORDER else 0
    rotation = SPEAKER_ORDER[start:] + SPEAKER_ORDER[:start]
    return next(name for name in rotation if name in candidates and turns[name] == fewest)


def apply_moderator_rules(
    decision: ModeratorDecision, turns: Counter[str], last_speaker: str | None
) -> tuple[bool, str | None]:
    """The moderator's decision after the hard rules: `(converged, next_speaker)`."""
    if decision.converged and all(turns[name] >= MIN_TURNS_BEFORE_CONVERGENCE for name in SPEAKER_ORDER):
        return True, None
    if decision.next_speaker is not None and decision.next_speaker != last_speaker:
        return False, decision.next_speaker
    return False, _least_heard(turns, last_speaker)


def _no_decision() -> ModeratorDecision:
    return ModeratorDecision(converged=False, next_speaker=None, reason=_NO_DECISION_REASON)


@with_usage(role="moderator")
def moderator_node(state: IdeaDiscussionState) -> dict[str, Any]:
    history = state["discussion_history"]
    turns = _turn_counts(history)
    last = _last_speaker(history)
    runtime = llm.get_runtime("moderator")
    prompt = (
        f"Anchor idea:\n{state['user_idea']}\n\n"
        "Panel transcript so far (chronological):\n"
        f"{history_markdown(history)}\n\n"
        f"Turns so far: {', '.join(f'{name}: {turns[name]}' for name in SPEAKER_ORDER)}.\n"
        f"Last speaker: {last or 'nobody yet'}.\n"
        f"Turn budget: {state['turn_count']} of {state['max_rounds']} used.\n\n"
        "Decide whether the panel has converged and, if not, who speaks next."
    )
    decision = llm.invoke_structured(
        runtime,
        [SystemMessage(content=runtime.system_prompt), HumanMessage(content=prompt)],
        ModeratorDecision,
        fallback=_no_decision,
    )
    converged, next_speaker = apply_moderator_rules(decision, turns, last)
    update: dict[str, Any] = {
        "convergence": {"converged": converged, "reason": decision.reason.strip()},
        "stage": "discussion",
    }
    if next_speaker is not None:
        update["next_speaker"] = next_speaker
    return update


# ------------------------------------------------------------------- routing


def route_panel_start(state: IdeaDiscussionState) -> list[Send] | Literal["speaker_turn"]:
    if state["panel_mode"] == "round_robin":
        return "speaker_turn"
    return [
        Send(
            "opening_turn",
            OpeningInput(
                speaker=speaker,
                user_idea=state["user_idea"],
                preferences=state.get("preferences") or {},
                research=state.get("research") or {},
                max_rounds=state["max_rounds"],
            ),
        )
        for speaker in SPEAKER_ORDER
    ]


def route_after_turn(state: IdeaDiscussionState) -> Literal["speaker_turn", "moderator", "__end__"]:
    """After any turn: stop at the turn cap, else the next turn (round robin) or the moderator."""
    if state["turn_count"] >= state["max_rounds"]:
        return _END
    return "speaker_turn" if state["panel_mode"] == "round_robin" else "moderator"


def route_after_moderator(state: IdeaDiscussionState) -> Literal["speaker_turn", "__end__"]:
    return _END if state["convergence"]["converged"] else "speaker_turn"


def build_panel_subgraph() -> CompiledStateGraph:
    """The panel, compiled to be mounted as the parent graph's `panel` node (it inherits the
    parent's checkpointer, so a crash mid-panel resumes from the last finished turn)."""
    builder = StateGraph(IdeaDiscussionState, input_schema=PanelInput)
    builder.add_node("opening_turn", opening_turn_node, input_schema=OpeningInput, retry_policy=LLM_RETRY)
    builder.add_node("merge_openings", merge_openings_node)
    builder.add_node("moderator", moderator_node, retry_policy=LLM_RETRY)
    builder.add_node("speaker_turn", discussion_node, retry_policy=LLM_RETRY)

    builder.add_conditional_edges(START, route_panel_start, ["opening_turn", "speaker_turn"])
    builder.add_edge("opening_turn", "merge_openings")
    builder.add_conditional_edges(
        "merge_openings", route_after_turn, {"moderator": "moderator", END: END}
    )
    builder.add_conditional_edges(
        "speaker_turn",
        route_after_turn,
        {"speaker_turn": "speaker_turn", "moderator": "moderator", END: END},
    )
    builder.add_conditional_edges(
        "moderator", route_after_moderator, {"speaker_turn": "speaker_turn", END: END}
    )
    return builder.compile()

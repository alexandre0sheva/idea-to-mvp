from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from idea_to_mvp import llm
from idea_to_mvp.nodes.common import history_markdown
from idea_to_mvp.roles import SPEAKER_NAME_TOKEN, SPEAKER_ORDER
from idea_to_mvp.state import IdeaDiscussionState
from idea_to_mvp.usage import with_usage

EMPTY_TURN_TEXT = "I could not produce a usable response for this turn."


def _next_speaker(current_speaker: str) -> str:
    idx = SPEAKER_ORDER.index(current_speaker)
    return SPEAKER_ORDER[(idx + 1) % len(SPEAKER_ORDER)]


def round_rules(max_rounds: int, turn_count: int) -> str:
    """The round-awareness lines of a turn prompt (the last round must converge, not expand)."""
    speakers_count = len(SPEAKER_ORDER)
    total_rounds = max(1, max_rounds // speakers_count)
    current_round = min(total_rounds, turn_count // speakers_count + 1)
    rules = f"- This is your round {current_round} of {total_rounds} in this panel.\n"
    if current_round >= total_rounds:
        rules += (
            "- This is your FINAL round: converge instead of expanding. State your position on the open "
            "disagreements, what you would commit to building, and what you would cut. Do not open new threads.\n"
        )
    return rules


def speaker_message(speaker: str, text: str) -> AIMessage:
    return AIMessage(
        content=text or EMPTY_TURN_TEXT,
        name=SPEAKER_NAME_TOKEN.get(speaker, speaker.lower().replace(" ", "_")),
    )


@with_usage(role=lambda state: SPEAKER_NAME_TOKEN[state["next_speaker"]])
def discussion_node(state: IdeaDiscussionState) -> dict[str, Any]:
    speaker = state["next_speaker"]
    runtime = llm.get_runtime(SPEAKER_NAME_TOKEN[speaker])

    thread_md = history_markdown(state["discussion_history"])
    round_rules_text = round_rules(state["max_rounds"], state["turn_count"])
    turn_prompt = (
        f"Anchor idea:\n{state['user_idea']}\n\n"
        "Panel transcript so far (chronological):\n"
        f"{thread_md}\n\n"
        f"You are **{speaker}**. Write the next panel turn.\n"
        "Rules for this turn:\n"
        f"{round_rules_text}"
        "- Explicitly reference at least one prior panelist by role name (PM, Tech Lead, Skeptic).\n"
        "- Build on or challenge a concrete claim from the transcript.\n"
        "- Add net-new decisions/assumptions/tradeoffs rather than repeating prior text.\n"
        "- Keep it concise, structured, and decision-oriented.\n"
        "- Use at most 2 short sections and at most 6 bullets total.\n"
    )
    text = llm.invoke_text(
        runtime,
        [
            SystemMessage(content=runtime.system_prompt),
            HumanMessage(content=turn_prompt + "\n\nReturn the final answer text explicitly."),
        ],
    )
    return {
        "discussion_history": [speaker_message(speaker, text)],
        "next_speaker": _next_speaker(speaker),
        "turn_count": state["turn_count"] + 1,
        "stage": "discussion",
    }


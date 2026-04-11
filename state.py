from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

DiscussionPhase = Literal["idea", "answers"]
SpeakerName = Literal["PM", "Tech Lead", "Skeptic"]


class IdeaDiscussionState(TypedDict):
    """State shared by all nodes in the orchestrator graph."""

    user_idea: str
    discussion_history: Annotated[list[BaseMessage], add_messages]
    summary: str
    generated_questions: list[str]
    user_answers: str
    architecture: str
    phase: DiscussionPhase
    next_speaker: SpeakerName
    max_rounds: int
    turn_count: int

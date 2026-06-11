from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

Stage = Literal[
    "discussion",
    "summary",
    "answers",
    "architecture",
    "plan_gate",
    "plan_bundle",
    "done",
]
SpeakerName = Literal["PM", "Tech Lead", "Skeptic"]


class PlanDecision(TypedDict):
    """Structured result of the plan gate interrupt."""

    generate: bool
    notes: str


class IdeaDiscussionState(TypedDict):
    """State shared by all nodes in the orchestrator graph."""

    user_idea: str
    discussion_history: Annotated[list[BaseMessage], add_messages]
    summary: str
    generated_questions: list[str]
    user_answers: str
    architecture: str
    plan_offer_question: str
    plan_decision: PlanDecision
    project_bundle_dir: str
    project_bundle_files: list[str]
    project_bundle_summary: str
    stage: Stage
    next_speaker: SpeakerName
    max_rounds: int
    turn_count: int

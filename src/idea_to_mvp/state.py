from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict, cast

from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.graph.message import add_messages

from idea_to_mvp.roles import SPEAKER_ORDER
from idea_to_mvp.usage import UsageRecord

Stage = Literal[
    "discussion",
    "summary",
    "answers",
    "architecture",
    "arch_choice",
    "strategy",
    "plan_gate",
    "plan_bundle",
    "implement_gate",
    "implementation",
    "verification",
    "report",
    "done",
]
SpeakerName = Literal["PM", "Tech Lead", "Skeptic"]


class PlanDecision(TypedDict):
    """Structured result of the plan gate interrupt."""

    generate: bool
    notes: str


class ArchChoice(TypedDict):
    """Structured result of the architecture choice interrupt."""

    option: str  # "A" | "B"
    notes: str


class ImplementDecision(TypedDict):
    """Structured result of the implementation gate interrupt."""

    implement: bool
    notes: str


class VerificationResult(TypedDict):
    """Outcome of the verification stage."""

    passed: bool
    attempts: int
    report: str


class IdeaDiscussionState(TypedDict):
    """State shared by all nodes in the orchestrator graph."""

    user_idea: str
    discussion_history: Annotated[list[BaseMessage], add_messages]
    summary: str
    questions: list[dict[str, Any]]  # MvpQuestion dumps (schemas.py)
    generated_questions: list[str]  # the same questions rendered as numbered lines
    user_answers: str
    architecture: str  # markdown rendered from architecture_proposal
    architecture_proposal: dict[str, Any]  # ArchitectureProposal dump
    arch_choice: ArchChoice
    execution_strategy: dict[str, Any]  # ExecutionStrategy dump
    plan_decision: PlanDecision
    implement_decision: ImplementDecision
    project_bundle_dir: str
    project_bundle_files: list[str]
    project_bundle_summary: str
    workspace_dir: str
    implementation_log: str
    verification: VerificationResult
    delivery_report: str
    usage: Annotated[list[UsageRecord], operator.add]  # model calls, accumulated over the run
    stage: Stage
    next_speaker: SpeakerName
    max_rounds: int
    turn_count: int


def make_initial_state(idea: str, rounds: int) -> IdeaDiscussionState:
    """Fresh state for a new pipeline run; the single place every field gets its default."""
    return {
        "user_idea": idea,
        "discussion_history": [HumanMessage(content=idea)],
        "summary": "",
        "questions": [],
        "generated_questions": [],
        "user_answers": "",
        "architecture": "",
        "architecture_proposal": {},
        "arch_choice": {"option": "", "notes": ""},
        "execution_strategy": {"mode": "", "reasoning": "", "workstreams": []},
        "plan_decision": {"generate": False, "notes": ""},
        "implement_decision": {"implement": False, "notes": ""},
        "project_bundle_dir": "",
        "project_bundle_files": [],
        "project_bundle_summary": "",
        "workspace_dir": "",
        "implementation_log": "",
        "verification": {"passed": False, "attempts": 0, "report": ""},
        "delivery_report": "",
        "usage": [],
        "stage": "discussion",
        "next_speaker": cast(SpeakerName, SPEAKER_ORDER[0]),
        "max_rounds": rounds * len(SPEAKER_ORDER),
        "turn_count": 0,
    }

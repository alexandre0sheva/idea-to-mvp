from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

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


class Workstream(TypedDict):
    """One independent slice of implementation work."""

    name: str
    focus: str
    deliverables: str


class ExecutionStrategy(TypedDict):
    """How implementation agents should be organized."""

    mode: str  # "subagents" | "agent_team"
    reasoning: str
    workstreams: list[Workstream]


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
    generated_questions: list[str]
    user_answers: str
    architecture: str
    arch_choice: ArchChoice
    execution_strategy: ExecutionStrategy
    plan_offer_question: str
    plan_decision: PlanDecision
    implement_decision: ImplementDecision
    project_bundle_dir: str
    project_bundle_files: list[str]
    project_bundle_summary: str
    workspace_dir: str
    implementation_log: str
    verification: VerificationResult
    delivery_report: str
    stage: Stage
    next_speaker: SpeakerName
    max_rounds: int
    turn_count: int

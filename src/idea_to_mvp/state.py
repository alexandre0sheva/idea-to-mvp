from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, NotRequired, TypedDict, TypeVar, cast

from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.graph.message import add_messages

from idea_to_mvp.config import PanelMode
from idea_to_mvp.implementation.progress import TaskResult
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
    parallel: NotRequired[int]  # tasks to run at once, chosen at the gate; 0 or absent = IMPLEMENTER_MAX_PARALLEL


class IterateDecision(TypedDict):
    """Structured result of the iterate gate interrupt."""

    iterate: bool
    feedback: str


class Convergence(TypedDict):
    """The panel moderator's latest verdict on whether the debate has converged."""

    converged: bool
    reason: str


_V = TypeVar("_V")


def merge_dicts(left: dict[str, _V], right: dict[str, _V]) -> dict[str, _V]:
    """Reducer for channels that parallel branches fill one key at a time."""
    return {**left, **right}


class VerificationResult(TypedDict):
    """Outcome of the verification stage."""

    passed: bool
    attempts: int  # fix attempts made so far
    report: str
    lanes: list[dict[str, Any]]  # one LaneReport dump per lane (tests, quality, requirements)


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
    blueprint_docs: Annotated[dict[str, str], merge_dicts]  # relative path -> generated document
    blueprint_review: dict[str, Any]  # {approved, revisions, issues}: the critic's latest verdict
    project_bundle_dir: str
    project_bundle_files: list[str]
    project_bundle_summary: str
    workspace_dir: str
    implementation_log: str
    finished_tasks: Annotated[dict[str, TaskResult], merge_dicts]  # sessions awaiting their merge (parallel engine)
    task_results: Annotated[dict[str, TaskResult], merge_dicts]  # plan task id -> its implementation result
    lane_reports: Annotated[dict[str, dict[str, Any]], merge_dicts]  # lane -> LaneReport dump, filled in parallel
    verification: VerificationResult
    delivery_report: str
    delivery_zip: str  # the latest delivery bundle (a zip of the project)
    iteration: int  # the version being built: 1 = the first delivery (v0.1), 2 = v0.2, ...
    change_requests: list[str]  # the user's feedback, one entry per iteration after the first
    iterate_decision: IterateDecision
    spent_before_iteration: float  # cost of earlier versions: each iteration has its own whole-run budget
    usage: Annotated[list[UsageRecord], operator.add]  # model calls, accumulated over the run
    stage: Stage
    next_speaker: SpeakerName
    max_rounds: int  # the panel's turn budget: rounds x speakers
    turn_count: int
    panel_mode: PanelMode
    autopilot: bool  # the answers, architecture, and plan gates decide for themselves; implement never does
    opening_turns: Annotated[dict[str, str], merge_dicts]  # speaker -> opening statement, filled in parallel
    convergence: Convergence


def make_initial_state(
    idea: str, rounds: int, *, panel_mode: PanelMode = "moderated", autopilot: bool = False
) -> IdeaDiscussionState:
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
        "blueprint_docs": {},
        "blueprint_review": {"approved": False, "revisions": 0, "issues": []},
        "project_bundle_dir": "",
        "project_bundle_files": [],
        "project_bundle_summary": "",
        "workspace_dir": "",
        "implementation_log": "",
        "finished_tasks": {},
        "task_results": {},
        "lane_reports": {},
        "verification": {"passed": False, "attempts": 0, "report": "", "lanes": []},
        "delivery_report": "",
        "delivery_zip": "",
        "iteration": 1,
        "change_requests": [],
        "iterate_decision": {"iterate": False, "feedback": ""},
        "spent_before_iteration": 0.0,
        "usage": [],
        "stage": "discussion",
        "next_speaker": cast(SpeakerName, SPEAKER_ORDER[0]),
        "max_rounds": rounds * len(SPEAKER_ORDER),
        "turn_count": 0,
        "panel_mode": panel_mode,
        "autopilot": autopilot,
        "opening_turns": {},
        "convergence": {"converged": False, "reason": ""},
    }

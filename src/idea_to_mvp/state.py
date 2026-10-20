from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, NotRequired, TypedDict, TypeVar, cast

from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.graph.message import add_messages

from idea_to_mvp.config import PanelMode
from idea_to_mvp.implementation.integrity import IntegrityFinding
from idea_to_mvp.implementation.progress import TaskResult
from idea_to_mvp.roles import SPEAKER_ORDER
from idea_to_mvp.schemas import ProjectPreferences
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
    harness_tuning: NotRequired[bool]  # tune the harness on a pilot task first; absent = the HARNESS_TUNING setting


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
    integrity: NotRequired[str]  # the TEST_INTEGRITY mode the verdict was given under (block / warn / off)
    # Why the loop stopped for a cause no change to the project can fix (a blocked domain, a missing credential).
    environment: NotRequired[str]
    history: NotRequired[list[dict[str, Any]]]  # one `fixes.FixAttempt` per fix attempt of this version


class IdeaDiscussionState(TypedDict):
    """State shared by all nodes in the orchestrator graph."""

    user_idea: str
    preferences: dict[str, Any]  # a `ProjectPreferences` dump: what the user stated up front
    research: dict[str, Any]  # a `ResearchBrief` dump ({} when research is off or found nothing)
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
    # What sessions did to the tests (`implementation/integrity.py`), by finding id, across every version.
    integrity_findings: Annotated[dict[str, IntegrityFinding], merge_dicts]
    integrity_test_count: int | None  # tests the last test report listed: a later drop is a finding
    # The held-out acceptance suite of the version being built: {iteration, dir, files, covers, note}; {} when off.
    acceptance: dict[str, Any]
    # Behavioral Contrast on a pilot task (`nodes/harness_tuning.py`): {status, pilot, budget_usd, base, inv, dom,
    # compose, adopted, commit, changed_files, ledger}; filled by parallel branches, {} until the stage runs.
    harness_tuning: Annotated[dict[str, Any], merge_dicts]
    # The experience library as read before the blueprint was written (`library.snapshot`): {domain, index, lessons,
    # entries}; {} when EXPERIENCE_LIBRARY is off. The planner, the harness writer, and the harness editors read it.
    library: dict[str, Any]
    # What the latest delivery's reflection recorded (`nodes/reflect.py`): {status, domain, entry, lesson, edits,
    # dropped, rationale, note}; {} before the first delivery or with the library off.
    reflection: dict[str, Any]
    delivery_report: str
    delivery_zip: str  # the latest delivery bundle (a zip of the project)
    # Where the delivery was published (`publish.py`): {repo, url, visibility, branch, commit, tags, files, backend,
    # published_at}; {} until the user publishes it.
    publication: dict[str, Any]
    iteration: int  # the version being built: 1 = the first delivery (v0.1), 2 = v0.2, ...
    change_requests: list[str]  # the user's feedback, one entry per iteration after the first
    iterate_decision: IterateDecision
    no_change: dict[str, str]  # {request, reason} when the change planner found nothing to build, else {}
    cost_estimate: dict[str, Any]  # the implement gate's dollar range for the first version (a `CostRange` dump)
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
    idea: str,
    rounds: int,
    *,
    panel_mode: PanelMode = "moderated",
    autopilot: bool = False,
    preferences: ProjectPreferences | None = None,
) -> IdeaDiscussionState:
    """Fresh state for a new pipeline run; the single place every field gets its default."""
    return {
        "user_idea": idea,
        "preferences": (preferences or ProjectPreferences()).model_dump(),
        "research": {},
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
        "integrity_findings": {},
        "integrity_test_count": None,
        "acceptance": {},
        "harness_tuning": {},
        "library": {},
        "reflection": {},
        "delivery_report": "",
        "delivery_zip": "",
        "publication": {},
        "iteration": 1,
        "change_requests": [],
        "iterate_decision": {"iterate": False, "feedback": ""},
        "no_change": {},
        "cost_estimate": {},
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

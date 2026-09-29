"""Human-in-the-loop gates: each pauses the graph with `interrupt()` and normalizes the resume value."""

from __future__ import annotations

from typing import Any, Literal

from langgraph.types import interrupt

from idea_to_mvp.config import get_settings
from idea_to_mvp.state import (
    ArchChoice,
    IdeaDiscussionState,
    ImplementDecision,
    PlanDecision,
)

PLAN_OFFER_QUESTION = (
    "Should I generate an agent-ready project pack next with scoped `AGENTS.md` files and a full `plan.md` "
    "covering task order, data contracts, and test expectations for the MVP?"
)


def collect_answers_node(state: IdeaDiscussionState) -> dict[str, Any]:
    answers = interrupt(
        {
            "kind": "answers",
            "summary": state.get("summary", ""),
            "questions": state.get("generated_questions", []),
            "question_items": state.get("questions", []),
        }
    )
    return {"user_answers": str(answers or "").strip(), "stage": "answers"}


def arch_choice_node(state: IdeaDiscussionState) -> dict[str, Any]:
    decision = interrupt(
        {
            "kind": "arch_choice",
            "question": (
                "Which architecture option should the blueprint and implementation target: "
                "Option A (fast and maintainable) or Option B (performance and scale)?"
            ),
            "architecture": state.get("architecture", ""),
            "proposal": state.get("architecture_proposal", {}),
        }
    )
    if isinstance(decision, dict):
        option = str(decision.get("option") or "A").strip().upper()
        notes = str(decision.get("notes") or "").strip()
    else:
        option = str(decision or "A").strip().upper()
        notes = ""
    if option not in ("A", "B"):
        option = "A"
    arch_choice: ArchChoice = {"option": option, "notes": notes}
    return {"arch_choice": arch_choice, "stage": "arch_choice"}


def plan_gate_node(state: IdeaDiscussionState) -> dict[str, Any]:
    decision = interrupt(
        {
            "kind": "plan_gate",
            "question": PLAN_OFFER_QUESTION,
            "architecture": state.get("architecture", ""),
        }
    )
    if isinstance(decision, dict):
        generate = bool(decision.get("generate"))
        notes = str(decision.get("notes") or "").strip()
    else:
        generate = bool(decision)
        notes = ""
    plan_decision: PlanDecision = {"generate": generate, "notes": notes}
    return {"plan_decision": plan_decision, "stage": "plan_bundle" if generate else "done"}


def route_after_plan_gate(state: IdeaDiscussionState) -> Literal["plan_bundle", "__end__"]:
    if state.get("plan_decision", {}).get("generate"):
        return "plan_bundle"
    return "__end__"


def implement_gate_node(state: IdeaDiscussionState) -> dict[str, Any]:
    settings = get_settings()
    question = (
        f"The blueprint pack is ready in `{state.get('project_bundle_dir', '')}`. "
        "Should I start the implementation stage now? Autonomous coding agents "
        f"(model `{settings.implementer_model}`) will build the project in `{settings.projects_dir}`, then a "
        "verification agent runs its test suite with up to "
        f"{settings.max_fix_attempts} fix attempt(s). This spends real Anthropic API tokens "
        f"(budget cap ${settings.implementer_max_budget_usd:.2f} per agent session) and the agents run "
        "with file/shell access inside that workspace."
    )
    decision = interrupt(
        {
            "kind": "implement_gate",
            "question": question,
            "bundle_dir": state.get("project_bundle_dir", ""),
        }
    )
    if isinstance(decision, dict):
        implement = bool(decision.get("implement"))
        notes = str(decision.get("notes") or "").strip()
    else:
        implement = bool(decision)
        notes = ""
    implement_decision: ImplementDecision = {"implement": implement, "notes": notes}
    return {"implement_decision": implement_decision, "stage": "implementation" if implement else "done"}


def route_after_implement_gate(state: IdeaDiscussionState) -> Literal["implementer", "__end__"]:
    if state.get("implement_decision", {}).get("implement"):
        return "implementer"
    return "__end__"

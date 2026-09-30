"""Human-in-the-loop gates: each pauses the graph with `interrupt()` and normalizes the resume value."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from langgraph.types import interrupt

from idea_to_mvp.config import get_settings
from idea_to_mvp.implementation.options import SandboxStatus, sandbox_status
from idea_to_mvp.implementation.scheduler import dag_width
from idea_to_mvp.plan import load_plan
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


AUTOPILOT_NOTE = "Autopilot: the architect's recommendation."


def _suggested_answers(state: IdeaDiscussionState) -> str | None:
    """The questions' own suggested answers as the numbered list a user would type, if every one has one."""
    items = state.get("questions") or []
    suggestions = [str(item.get("suggested_answer") or "").strip() for item in items]
    if not items or not all(suggestions):
        return None
    return "\n".join(f"{number}. {suggestion}" for number, suggestion in enumerate(suggestions, start=1))


def collect_answers_node(state: IdeaDiscussionState) -> dict[str, Any]:
    if state.get("autopilot") and (suggested := _suggested_answers(state)):
        return {"user_answers": suggested, "stage": "answers"}
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
    if state.get("autopilot"):
        recommended = str((state.get("architecture_proposal") or {}).get("recommendation") or "A").strip().upper()
        auto: ArchChoice = {"option": recommended if recommended in ("A", "B") else "A", "notes": AUTOPILOT_NOTE}
        return {"arch_choice": auto, "stage": "arch_choice"}
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
    if state.get("autopilot"):
        auto: PlanDecision = {"generate": True, "notes": "Autopilot: generating the pack."}
        return {"plan_decision": auto, "stage": "plan_bundle"}
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


def _gate_warning(settings: Any, status: SandboxStatus) -> str:
    warnings: list[str] = []
    if settings.demo_mode:
        return ""
    if settings.implementer_permission_mode == "bypassPermissions":
        warnings.append(
            "Permission mode `bypassPermissions`: the agents run every tool without asking."
        )
    if not status.enabled:
        warnings.append(
            "The sandbox is off or unavailable: shell commands run with your full user permissions "
            "(only the tool-call guard applies). Use a container or VM for ideas you do not trust."
        )
    return " ".join(warnings)


def _plan_shape(state: IdeaDiscussionState, max_parallel: int) -> tuple[int, int, str]:
    """(task count, dag width, one sentence for the question) of the pack's plan, for the cost estimate."""
    try:
        plan = load_plan((Path(state.get("project_bundle_dir", "")) / "plan.json").read_text(encoding="utf-8"))
    except OSError:
        plan = None
    if plan is None or not plan.tasks:
        return 0, 0, ""
    width = dag_width(plan)
    if (state.get("execution_strategy") or {}).get("mode") != "agent_team":
        return len(plan.tasks), width, ""  # a lead session builds everything in one go
    if max_parallel <= 1 or width <= 1:
        how = "they will be built one at a time"
    else:
        how = f"{min(width, max_parallel)} of them can run in parallel (each in its own git worktree)"
    return len(plan.tasks), width, f"The plan has {len(plan.tasks)} tasks; {how}. "


def implement_gate_node(state: IdeaDiscussionState) -> dict[str, Any]:
    settings = get_settings()
    status = sandbox_status(settings)
    task_count, width, shape = _plan_shape(state, settings.implementer_max_parallel)
    question = (
        f"The blueprint pack is ready in `{state.get('project_bundle_dir', '')}`. "
        "Should I start the implementation stage now? "
        f"{shape}Autonomous coding agents "
        f"(model `{settings.implementer_model}`) will build the project in `{settings.projects_dir}`, then a "
        "verification agent runs its test suite with up to "
        f"{settings.max_fix_attempts} fix attempt(s). This spends real Anthropic API tokens "
        f"(whole-run budget cap ${settings.implementer_max_total_usd:.2f}, at most "
        f"${settings.implementer_max_task_usd:.2f} per task). "
        f"Sandbox: {status.summary}. Permission mode: `{settings.implementer_permission_mode}`."
    )
    review_notes = [
        str(issue.get("description") or "") for issue in (state.get("blueprint_review") or {}).get("issues") or []
    ]
    if review_notes:
        question += (
            f" The blueprint review left {len(review_notes)} open review note(s): read `REVIEW.md` in the pack "
            "before approving."
        )
    decision = interrupt(
        {
            "kind": "implement_gate",
            "question": question,
            "bundle_dir": state.get("project_bundle_dir", ""),
            "review_issues": review_notes,
            "sandbox": {"enabled": status.enabled, "mode": settings.implementer_sandbox, "summary": status.summary},
            "permission_mode": settings.implementer_permission_mode,
            "strategy_mode": (state.get("execution_strategy") or {}).get("mode") or "subagents",
            "model": settings.implementer_model,
            "max_total_usd": settings.implementer_max_total_usd,
            "max_task_usd": settings.implementer_max_task_usd,
            "max_fix_attempts": settings.max_fix_attempts,
            "task_count": task_count,
            "dag_width": width,
            "max_parallel": settings.implementer_max_parallel,
            "allowed_domains": list(settings.implementer_allowed_domains),
            "warning": _gate_warning(settings, status),
        }
    )
    parallel = 0
    if isinstance(decision, dict):
        implement = bool(decision.get("implement"))
        notes = str(decision.get("notes") or "").strip()
        try:
            parallel = max(0, int(decision.get("parallel") or 0))
        except (TypeError, ValueError):
            parallel = 0
    else:
        implement = bool(decision)
        notes = ""
    implement_decision: ImplementDecision = {"implement": implement, "notes": notes, "parallel": parallel}
    return {"implement_decision": implement_decision, "stage": "implementation" if implement else "done"}


def route_after_implement_gate(state: IdeaDiscussionState) -> Literal["prepare_workspace", "__end__"]:
    if state.get("implement_decision", {}).get("implement"):
        return "prepare_workspace"
    return "__end__"

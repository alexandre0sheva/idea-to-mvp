from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from idea_to_mvp import llm
from idea_to_mvp.blueprints import build_bundle_context, create_project_bundle
from idea_to_mvp.config import get_settings
from idea_to_mvp.schemas import ArchitectureProposal, render_option_markdown
from idea_to_mvp.state import IdeaDiscussionState
from idea_to_mvp.usage import with_usage


@with_usage(role="architect")
def plan_bundle_node(state: IdeaDiscussionState) -> dict[str, Any]:
    runtime = llm.get_runtime("architect")
    strategy = state.get("execution_strategy") or None
    chosen_details = ""
    if state.get("architecture_proposal"):
        proposal = ArchitectureProposal.model_validate(state["architecture_proposal"])
        chosen_details = render_option_markdown(proposal, state.get("arch_choice", {}).get("option") or "A")
    context_block = build_bundle_context(
        user_idea=state["user_idea"],
        summary=state.get("summary", ""),
        questions=state.get("generated_questions", []),
        user_answers=state.get("user_answers", ""),
        architecture=state.get("architecture", ""),
        arch_choice=dict(state.get("arch_choice") or {}),
        chosen_option_details=chosen_details,
        strategy=strategy,
        planning_request=state.get("plan_decision", {}).get("notes", ""),
    )

    def generate_doc(system_prompt: str, user_prompt: str) -> str:
        return llm.invoke_text(
            runtime,
            [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)],
        )

    bundle_dir, created_files, summary = create_project_bundle(
        root=get_settings().blueprints_dir,
        user_idea=state["user_idea"],
        context_block=context_block,
        strategy=strategy,
        generate_doc=generate_doc,
    )
    return {
        "project_bundle_dir": str(bundle_dir),
        "project_bundle_files": created_files,
        "project_bundle_summary": summary,
        "stage": "plan_bundle",
    }

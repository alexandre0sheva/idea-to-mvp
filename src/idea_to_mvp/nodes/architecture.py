from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from idea_to_mvp import llm
from idea_to_mvp.nodes.common import preferences_block
from idea_to_mvp.schemas import (
    ArchitectureOption,
    ArchitectureProposal,
    render_architecture_markdown,
)
from idea_to_mvp.state import IdeaDiscussionState
from idea_to_mvp.usage import with_usage


def fallback_proposal() -> ArchitectureProposal:
    """Generic two-option proposal, used when the model cannot produce a valid one."""
    return ArchitectureProposal(
        option_a=ArchitectureOption(
            key="A",
            name="Modular monolith (generic default)",
            style="Single deployable app with clear module boundaries",
            stack=["Pick the team's strongest web stack"],
            persistence="One relational database",
            integrations=["Only those required by the core journey"],
            security_baseline="Managed auth, input validation, backups",
            tradeoffs=["Scales vertically first"],
            limits="Comfortable for early users; revisit at real load",
            anchored_constraints=["Generic default: review against your answers"],
        ),
        option_b=ArchitectureOption(
            key="B",
            name="Service-oriented with async workers (generic default)",
            style="API service plus background workers and a cache",
            stack=["Typed API service", "Queue and worker runtime"],
            persistence="Managed relational database plus cache",
            integrations=["Message queue", "Object storage"],
            security_baseline="Centralised auth, rate limiting, observability",
            tradeoffs=["More moving parts to operate"],
            limits="Scales horizontally; higher upfront cost",
            anchored_constraints=["Generic default: review against your answers"],
        ),
        shared_components=["Domain model", "Authentication"],
        recommendation="A",
        recommendation_rationale="The architect could not produce a tailored proposal; the simplest option ships fastest.",
        biggest_tradeoff="Vertical scaling limits",
        rollout=["Launch the modular monolith", "Extract workers where load demands"],
    )


@with_usage(role="architect")
def architect_node(state: IdeaDiscussionState) -> dict[str, Any]:
    architect = llm.get_runtime("architect")
    questions = "\n".join(state.get("generated_questions", [])) or "No explicit questions provided."
    proposal = llm.invoke_structured(
        architect,
        [
            SystemMessage(content=architect.system_prompt),
            HumanMessage(
                content=(
                    f"Initial user idea:\n{state['user_idea'].strip()}\n\n"
                    f"{preferences_block(state.get('preferences'))}"
                    f"Discussion summary:\n{state.get('summary', '').strip()}\n\n"
                    f"Questions asked to user:\n{questions}\n\n"
                    f"User responses:\n{state.get('user_answers', '').strip()}\n\n"
                    "Create the two architecture options now. Keep this at high level for MVP planning."
                )
            ),
        ],
        ArchitectureProposal,
        fallback=fallback_proposal,
    )
    return {
        "architecture_proposal": proposal.model_dump(),
        "architecture": render_architecture_markdown(proposal),
        "stage": "architecture",
    }

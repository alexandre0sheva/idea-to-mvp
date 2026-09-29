from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from idea_to_mvp import llm
from idea_to_mvp.schemas import ExecutionStrategy, Workstream
from idea_to_mvp.state import IdeaDiscussionState
from idea_to_mvp.usage import with_usage


def fallback_strategy() -> ExecutionStrategy:
    """Safest strategy for a small MVP, used when the model cannot produce a valid one."""
    return ExecutionStrategy(
        mode="subagents",
        reasoning=(
            "Strategy output could not be validated; defaulting to a single lead session with specialized "
            "subagents, which is the safest mode for a small MVP."
        ),
        workstreams=[
            Workstream(
                name="core-product",
                focus="Implement the MVP end to end following plan.md.",
                deliverables="Working application code with tests.",
            ),
            Workstream(
                name="quality",
                focus="Test coverage, fixtures, and verification of every task in plan.md.",
                deliverables="Passing unit/integration test suite.",
            ),
        ],
    )


@with_usage(role="strategy")
def strategy_node(state: IdeaDiscussionState) -> dict[str, Any]:
    runtime = llm.get_runtime("strategy")
    arch_choice = state.get("arch_choice", {})
    strategy = llm.invoke_structured(
        runtime,
        [
            SystemMessage(content=runtime.system_prompt),
            HumanMessage(
                content=(
                    f"Idea:\n{state.get('user_idea', '').strip()}\n\n"
                    f"Discussion summary:\n{state.get('summary', '').strip() or 'No summary.'}\n\n"
                    f"User answers:\n{state.get('user_answers', '').strip() or 'No answers.'}\n\n"
                    f"Architecture options:\n{state.get('architecture', '').strip() or 'No architecture.'}\n\n"
                    f"User chose option: {arch_choice.get('option') or 'A'}\n"
                    f"User notes: {arch_choice.get('notes') or 'None.'}\n\n"
                    "Decide the execution mode and workstreams now."
                )
            ),
        ],
        ExecutionStrategy,
        fallback=fallback_strategy,
    )
    return {"execution_strategy": strategy.model_dump(), "stage": "strategy"}

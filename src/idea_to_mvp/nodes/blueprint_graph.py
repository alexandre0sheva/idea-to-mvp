"""The blueprint pack as a subgraph: dependency waves, a structured plan, a critic, a revise loop.

```
START → wave_1 ─Send×n→ doc_1 → wave_2 ─Send×n→ doc_2 ┐
         PRD, ARCHITECTURE        README, AGENTS×4      ├→ wave_3 ─Send×n→ doc_3 → review ─┬→ write → END
                                  plan ─────→ plan_writer ┘   .claude/agents/*          ↑     │ blocker, revisions left
                                                                                        │     ↓
                                                   rewrite_doc / rewrite_plan ←─Send─ revise
```

Each `wave_n` node is a no-op that only exists to fan out: its conditional edge sends one task per
document of that wave, and the static edge out of the workers fires once all of them are done. Every
document is written from the shared context plus the upstream documents it `needs`
(`blueprints.BundleFileSpec`), so the plan sees the real PRD and the subagents see the real plan. The
plan is typed data (`plan_writer`, validated and repaired once). `review` is an evaluator-optimizer
step: the deterministic plan validation plus a critic model check the documents against each other;
while blockers name documents and `MAX_BLUEPRINT_REVISIONS` allows, `revise` regenerates only those
documents with the issues appended and the pack is reviewed again. An empty wave (no workstreams means
no wave 3) is skipped.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Send

from idea_to_mvp.blueprint_review import format_issues, issues_for, revision_targets
from idea_to_mvp.blueprints import PLAN_JSON_PATH, WAVES, BundleFileSpec, bundle_file_plan
from idea_to_mvp.nodes.blueprint import (
    DocInput,
    blueprint_context,
    generate_doc_node,
    generate_plan_node,
    review_issues_of,
    review_node,
    revise_node,
    will_revise,
    write_bundle_node,
)
from idea_to_mvp.resilience import LLM_RETRY
from idea_to_mvp.state import IdeaDiscussionState


class BlueprintInput(TypedDict):
    """What the parent hands the blueprint step. Leaves out `usage`, `blueprint_docs`, and
    `blueprint_review` so the subgraph starts those channels empty and returns only what it added
    (see `nodes/panel.py`)."""

    user_idea: str
    discussion_history: Annotated[list[BaseMessage], add_messages]
    summary: str
    generated_questions: list[str]
    user_answers: str
    architecture: str
    architecture_proposal: dict[str, Any]
    arch_choice: dict[str, Any]
    execution_strategy: dict[str, Any]
    plan_decision: dict[str, Any]


def _next_step(wave: int) -> str:
    return f"wave_{wave + 1}" if wave < len(WAVES) else "review"


def _worker_for(spec: BundleFileSpec, doc_node: str, plan_node: str) -> str:
    return plan_node if spec.structured else doc_node


def _send(
    spec: BundleFileSpec,
    node: str,
    state: IdeaDiscussionState,
    context_block: str,
    *,
    issues: str = "",
    previous: str = "",
) -> Send:
    docs = state.get("blueprint_docs") or {}
    return Send(
        node,
        DocInput(
            path=spec.relative_path,
            strategy=state.get("execution_strategy") or None,
            context_block=context_block,
            upstream={path: docs[path] for path in spec.needs if path in docs},
            issues=issues,
            previous=previous,
        ),
    )


def _wave_router(wave: int) -> Callable[[IdeaDiscussionState], list[Send] | str]:
    def route(state: IdeaDiscussionState) -> list[Send] | str:
        specs = [s for s in bundle_file_plan(state.get("execution_strategy") or None) if s.wave == wave]
        if not specs:
            return _next_step(wave)
        context_block = blueprint_context(state)
        return [_send(s, _worker_for(s, f"doc_{wave}", "plan_writer"), state, context_block) for s in specs]

    route.__name__ = f"route_wave_{wave}"
    return route


def route_after_review(state: IdeaDiscussionState) -> Literal["revise", "write"]:
    return "revise" if will_revise(state) else "write"


def route_revisions(state: IdeaDiscussionState) -> list[Send]:
    """One rewrite task per document a blocker names, each with its issues and its previous version."""
    strategy = state.get("execution_strategy") or None
    specs = {s.relative_path: s for s in bundle_file_plan(strategy)}
    issues = review_issues_of(state.get("blueprint_review"))
    docs = state.get("blueprint_docs") or {}
    context_block = blueprint_context(state)
    sends = []
    for path in revision_targets(issues, list(specs)):
        spec = specs[path]
        previous = docs.get(PLAN_JSON_PATH if spec.structured else path, "")
        sends.append(
            _send(
                spec,
                _worker_for(spec, "rewrite_doc", "rewrite_plan"),
                state,
                context_block,
                issues=format_issues(issues_for(path, issues, list(specs))),
                previous=previous,
            )
        )
    return sends


def _fan_out(state: IdeaDiscussionState) -> dict[str, Any]:
    return {}


def build_blueprint_subgraph() -> CompiledStateGraph:
    """The blueprint pack, compiled to be mounted as the parent graph's `plan_bundle` node."""
    builder = StateGraph(IdeaDiscussionState, input_schema=BlueprintInput)
    for wave in WAVES:
        builder.add_node(f"wave_{wave}", _fan_out)
        builder.add_node(f"doc_{wave}", generate_doc_node, input_schema=DocInput, retry_policy=LLM_RETRY)
    builder.add_node("plan_writer", generate_plan_node, input_schema=DocInput, retry_policy=LLM_RETRY)
    builder.add_node("review", review_node, retry_policy=LLM_RETRY)
    builder.add_node("revise", revise_node)
    builder.add_node("rewrite_doc", generate_doc_node, input_schema=DocInput, retry_policy=LLM_RETRY)
    builder.add_node("rewrite_plan", generate_plan_node, input_schema=DocInput, retry_policy=LLM_RETRY)
    builder.add_node("write", write_bundle_node)

    builder.add_edge(START, "wave_1")
    for wave in WAVES:
        workers = [f"doc_{wave}", "plan_writer"] if wave == 2 else [f"doc_{wave}"]
        builder.add_conditional_edges(f"wave_{wave}", _wave_router(wave), [*workers, _next_step(wave)])
        for worker in workers:  # separate edges: the step after fires once per superstep, whichever ran
            builder.add_edge(worker, _next_step(wave))
    builder.add_conditional_edges("review", route_after_review, ["revise", "write"])
    builder.add_conditional_edges("revise", route_revisions, ["rewrite_doc", "rewrite_plan"])
    builder.add_edge("rewrite_doc", "review")
    builder.add_edge("rewrite_plan", "review")
    builder.add_edge("write", END)
    return builder.compile()

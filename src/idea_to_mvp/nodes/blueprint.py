"""Node functions of the blueprint subgraph (topology lives in `blueprint_graph.py`)."""

from __future__ import annotations

from typing import Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage

from idea_to_mvp import llm
from idea_to_mvp.blueprint_review import (
    CritiqueReport,
    Issue,
    finalize_report,
    plan_issues_as_issues,
    render_review_markdown,
    revision_targets,
)
from idea_to_mvp.blueprints import (
    MAX_UPSTREAM_CHARS,
    PLAN_JSON_PATH,
    PLAN_PATH,
    REVIEW_PATH,
    build_bundle_context,
    bundle_file_plan,
    prompt_for,
    truncate_document,
    write_bundle,
)
from idea_to_mvp.config import get_settings
from idea_to_mvp.plan import (
    Plan,
    fallback_plan,
    load_plan,
    render_plan_markdown,
    validate_plan,
    workstream_names,
)
from idea_to_mvp.schemas import ArchitectureProposal, render_option_markdown
from idea_to_mvp.state import IdeaDiscussionState
from idea_to_mvp.usage import with_usage

CRITIC_NOT_AVAILABLE = (
    "The automated cross-document review could not be completed; review the pack manually."
)


class DocInput(TypedDict):
    """Payload of one `Send(...)` to a document worker: which document to write and what it may read."""

    path: str
    strategy: dict[str, Any] | None
    context_block: str
    upstream: dict[str, str]  # the documents this one `needs`, already generated
    issues: str  # reviewer issues to fix, formatted ('' on the first pass)
    previous: str  # the previous version of this document ('' on the first pass)


def blueprint_context(state: IdeaDiscussionState) -> str:
    """The shared context block every blueprint document is written from."""
    chosen_details = ""
    if state.get("architecture_proposal"):
        proposal = ArchitectureProposal.model_validate(state["architecture_proposal"])
        chosen_details = render_option_markdown(proposal, state.get("arch_choice", {}).get("option") or "A")
    return build_bundle_context(
        user_idea=state["user_idea"],
        summary=state.get("summary", ""),
        questions=state.get("generated_questions", []),
        user_answers=state.get("user_answers", ""),
        architecture=state.get("architecture", ""),
        arch_choice=dict(state.get("arch_choice") or {}),
        chosen_option_details=chosen_details,
        strategy=state.get("execution_strategy") or None,
        planning_request=state.get("plan_decision", {}).get("notes", ""),
    )


def _spec_for(state: DocInput):
    return next(s for s in bundle_file_plan(state["strategy"]) if s.relative_path == state["path"])


@with_usage(role="architect")
def generate_doc_node(state: DocInput) -> dict[str, Any]:
    """Write one text document. An empty reply is replaced by the fallback here, so documents further
    down the DAG are written from exactly what will end up in the bundle."""
    spec = _spec_for(state)
    runtime = llm.get_runtime("architect")
    text = llm.invoke_text(
        runtime,
        [
            SystemMessage(content=spec.system_prompt),
            HumanMessage(
                content=prompt_for(
                    spec, state["context_block"], state["upstream"], issues=state["issues"], previous=state["previous"]
                )
            ),
        ],
    )
    return {"blueprint_docs": {spec.relative_path: text.strip() or spec.fallback.strip()}}


@with_usage(role="plan_writer")
def generate_plan_node(state: DocInput) -> dict[str, Any]:
    """Write the plan as typed data; the deterministic validator's findings get one repair attempt.

    Stores `plan.json` (the source of truth) and the rendered `plan.md` (what downstream documents read).
    """
    spec = _spec_for(state)
    strategy = state["strategy"]
    prd = state["upstream"].get("PRD.md", "")
    workstreams = workstream_names(strategy)
    runtime = llm.get_runtime("plan_writer")
    messages = [
        SystemMessage(content=runtime.system_prompt),
        HumanMessage(
            content=prompt_for(
                spec, state["context_block"], state["upstream"], issues=state["issues"], previous=state["previous"]
            )
        ),
    ]
    plan = llm.invoke_structured(runtime, messages, Plan, fallback=lambda: fallback_plan(strategy, prd))
    problems = validate_plan(plan, prd_markdown=prd, workstreams=workstreams)
    if problems:
        repaired = llm.invoke_structured(
            runtime,
            [
                *messages,
                HumanMessage(
                    content=(
                        "Your plan has these consistency problems:\n"
                        + "\n".join(f"- {problem}" for problem in problems)
                        + f"\n\nYour plan (JSON):\n{plan.model_dump_json(indent=2)}\n\n"
                        "Return the complete corrected plan: fix every problem and change nothing else."
                    )
                ),
            ],
            Plan,
            fallback=lambda: plan,
        )
        if len(validate_plan(repaired, prd_markdown=prd, workstreams=workstreams)) < len(problems):
            plan = repaired  # a repair that does not help is discarded
    return {
        "blueprint_docs": {
            PLAN_JSON_PATH: plan.model_dump_json(indent=2),
            PLAN_PATH: render_plan_markdown(plan),
        }
    }


# -------------------------------------------------------------- review + revise


def _known_paths(strategy: dict[str, Any] | None) -> list[str]:
    return [spec.relative_path for spec in bundle_file_plan(strategy)]


def _plan_problems(docs: dict[str, str], strategy: dict[str, Any] | None) -> list[str]:
    plan = load_plan(docs.get(PLAN_JSON_PATH))
    if plan is None:
        return ["plan.json is missing or is not a valid plan"]
    return validate_plan(plan, prd_markdown=docs.get("PRD.md", ""), workstreams=workstream_names(strategy))


def review_issues_of(review: dict[str, Any] | None) -> list[Issue]:
    return [Issue.model_validate(item) for item in (review or {}).get("issues") or []]


def will_revise(state: IdeaDiscussionState) -> bool:
    """More review rounds are allowed and a blocker names a document that can be regenerated."""
    review = state.get("blueprint_review") or {}
    if int(review.get("revisions") or 0) >= get_settings().max_blueprint_revisions:
        return False
    return bool(revision_targets(review_issues_of(review), _known_paths(state.get("execution_strategy") or None)))


def _unreviewed() -> CritiqueReport:
    return CritiqueReport(
        approved=True, issues=[Issue(severity="warning", file="pack", description=CRITIC_NOT_AVAILABLE)]
    )


def _critic_prompt(state: IdeaDiscussionState, docs: dict[str, str], paths: list[str]) -> str:
    reviewed = ["PRD.md", "ARCHITECTURE.md", PLAN_PATH, *[p for p in paths if p.startswith(".claude/agents/")]]
    sections = [
        f"## File: {path}\n\n{truncate_document(docs[path], MAX_UPSTREAM_CHARS)}" for path in reviewed if docs.get(path)
    ]
    return (
        f"Original idea:\n{state['user_idea'].strip()}\n\n"
        f"Valid file paths for issues: {', '.join(reviewed)}\n\n"
        + "\n\n".join(sections)
        + "\n\nReview the pack now and report every issue you find."
    )


@with_usage(role="blueprint_critic")
def review_node(state: IdeaDiscussionState) -> dict[str, Any]:
    """Cross-check the documents: the deterministic plan validation plus the critic model. The final
    pass (no more revisions possible) also writes the notes that remain into `REVIEW.md`."""
    docs = state.get("blueprint_docs") or {}
    strategy = state.get("execution_strategy") or None
    issues = plan_issues_as_issues(_plan_problems(docs, strategy))
    runtime = llm.get_runtime("blueprint_critic")
    report = finalize_report(
        llm.invoke_structured(
            runtime,
            [
                SystemMessage(content=runtime.system_prompt),
                HumanMessage(content=_critic_prompt(state, docs, _known_paths(strategy))),
            ],
            CritiqueReport,
            fallback=_unreviewed,
        )
    )
    issues += report.issues
    review = {
        "approved": not any(issue.severity == "blocker" for issue in issues),
        "revisions": int((state.get("blueprint_review") or {}).get("revisions") or 0),
        "issues": [issue.model_dump() for issue in issues],
    }
    update: dict[str, Any] = {"blueprint_review": review}
    if issues and not will_revise({**state, "blueprint_review": review}):
        update["blueprint_docs"] = {REVIEW_PATH: render_review_markdown(issues)}
    return update


def revise_node(state: IdeaDiscussionState) -> dict[str, Any]:
    review = dict(state["blueprint_review"])
    review["revisions"] = int(review.get("revisions") or 0) + 1
    return {"blueprint_review": review}


def write_bundle_node(state: IdeaDiscussionState) -> dict[str, Any]:
    review = state.get("blueprint_review") or {}
    bundle_dir, created_files, summary = write_bundle(
        root=get_settings().blueprints_dir,
        user_idea=state["user_idea"],
        docs=state.get("blueprint_docs") or {},
        strategy=state.get("execution_strategy") or None,
        review_issues=[issue.description for issue in review_issues_of(review)],
    )
    return {
        "project_bundle_dir": str(bundle_dir),
        "project_bundle_files": created_files,
        "project_bundle_summary": summary,
        "stage": "plan_bundle",
    }

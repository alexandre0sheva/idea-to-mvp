"""Blueprint v2 generation: the agent-ready project pack written before implementation.

This module owns the document prompts, the dependency waves, and the file layout of a project
bundle. It does not call any LLM itself: `nodes/blueprint_graph.py` generates the documents wave by
wave (each document sees the upstream documents it `needs`) and `write_bundle()` writes the result.
The plan is structured: `plan.json` (a validated `plan.Plan`) is the source of truth and `plan.md` is
rendered from it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from idea_to_mvp.plan import fallback_plan, load_plan, render_plan_markdown
from idea_to_mvp.roles import PLAN_WRITER_SYSTEM

# Upstream documents are injected into later prompts up to this many characters each.
MAX_UPSTREAM_CHARS = 12_000
TRUNCATED_MARKER = "[truncated]"

README_SYSTEM = (
    "You are writing the root README.md scaffold for a brand-new MVP project that coding agents will implement.\n"
    "Write it for future human contributors: project name and one-line pitch, problem and target users, "
    "feature list of the MVP, tech stack (from the chosen architecture), local setup and run instructions "
    "(best-guess commands for the chosen stack), test command, and project layout.\n"
    "Keep it under 400 words, use concrete commands, and do not invent features beyond the MVP scope."
)

PRD_SYSTEM = (
    "You are writing PRD.md, the product requirements document for an MVP, optimized for coding agents.\n"
    "Cover: problem statement, target users and primary personas, user stories for every MVP workflow "
    "(as 'As a ... I want ... so that ...'), functional requirements with priorities (P0/P1), explicit "
    "non-goals/out-of-scope list, success metrics, and open product questions.\n"
    "Number every requirement (R1, R2, ...) and put each on its own line in the form `R1 (P0): ...` so plan tasks "
    "and tests can reference it.\n"
    "Be precise and testable: every P0 requirement must be verifiable by a test."
)

ARCHITECTURE_DOC_SYSTEM = (
    "You are writing ARCHITECTURE.md for an MVP project, expanding the architecture option the user selected.\n"
    "Cover: system overview and component diagram (ASCII), each component's responsibility and boundaries, "
    "data model at entity level (names, key fields, relationships - no full DDL), API surface (endpoints/commands "
    "with one-line contracts), technology choices with one-line justifications, error-handling and logging "
    "strategy, security baseline, and how the architecture scales later.\n"
    "Stay consistent with the chosen option; mention rejected-option tradeoffs only in one short closing section."
)

ROOT_AGENTS_SYSTEM = (
    "You are writing a root AGENTS.md file for a brand-new MVP project folder.\n"
    "This file will be read by coding agents before they start implementation.\n"
    "Write concise, actionable instructions with short headings and bullets.\n"
    "Focus on: project goal, MVP boundaries, chosen architecture direction, delivery workflow, "
    "definition of done, testing bar, contract discipline, and handoff expectations.\n"
    "Best practices:\n"
    "- be concrete and directive\n"
    "- include a short execution checklist agents can follow\n"
    "- tell agents to read `PRD.md`, `ARCHITECTURE.md`, and `plan.md` first, then the scoped guides "
    "(`contracts/AGENTS.md`, `application/AGENTS.md`, `quality/AGENTS.md`)\n"
    "- mention that `.claude/agents/` contains specialized subagent definitions and `STRATEGY.json` "
    "describes the execution mode\n"
    "- keep it usable as an operating manual, not a product essay"
)

CONTRACTS_AGENTS_SYSTEM = (
    "You are writing `contracts/AGENTS.md` for a multi-agent MVP project.\n"
    "This file defines how agents create, use, and evolve data contracts safely.\n"
    "Write concise operational guidance covering: contract ownership, API/schema/event boundaries, versioning, "
    "compatibility rules, migration protocol, review checklist, and required contract artifacts.\n"
    "Include a small example contract template in markdown.\n"
    "Be strict, practical, and optimized for parallel agent work."
)

APPLICATION_AGENTS_SYSTEM = (
    "You are writing `application/AGENTS.md` for the main product implementation workstream.\n"
    "This file should guide agents building features across UI, backend, integrations, and business logic.\n"
    "Cover: how to slice work, how to consume contracts, how to avoid cross-task conflicts, acceptance criteria, "
    "migration discipline, observability expectations, and what must be updated before handoff.\n"
    "Use direct instructions and short checklists."
)

QUALITY_AGENTS_SYSTEM = (
    "You are writing `quality/AGENTS.md` for a multi-agent MVP delivery process.\n"
    "This file should define testing and verification expectations for every task.\n"
    "Cover: agent-run verification, required unit/integration tests, coverage goals, contract tests, "
    "fixture guidance, regression checks, release-readiness checks, and how to document residual risk.\n"
    "Make the testing standard explicit: each task must include unit/integration tests and target at least 80% "
    "coverage for the changed scope.\n"
    "Also require a single top-level test command that verifies the whole project, documented in README.md."
)

SUBAGENT_SYSTEM = (
    "You are writing the body of a Claude Code subagent definition for one implementation workstream.\n"
    "Write a focused system prompt (no YAML frontmatter - it is added separately) that tells this agent:\n"
    "its workstream mission and deliverables, which tasks of the upstream plan.md provided below belong to it "
    "(name them by number), which docs to read first "
    "(AGENTS.md, PRD.md, ARCHITECTURE.md, plan.md, contracts/AGENTS.md, quality/AGENTS.md), the contracts it "
    "must respect, the testing bar (tests per task, 80% coverage of changed scope), and how to hand off "
    "(update plan.md checkboxes/notes, keep the test suite green).\n"
    "Keep it under 250 words, direct, second-person."
)


@dataclass(frozen=True)
class BundleFileSpec:
    relative_path: str
    system_prompt: str
    instruction: str
    fallback: str
    frontmatter: str = ""
    # Generation order: documents in one wave are independent of each other and are written concurrently;
    # a wave starts once the previous one is done. 1: PRD, ARCHITECTURE. 2: everything that builds on them
    # (README, the four AGENTS guides, plan). 3: the per-workstream subagent definitions (they need the plan).
    wave: int = 1
    needs: tuple[str, ...] = ()  # upstream documents (by relative path) injected into this prompt
    structured: bool = False  # produced as typed data by the plan writer, not as free text


WAVES = (1, 2, 3)
PLAN_PATH = "plan.md"
PLAN_JSON_PATH = "plan.json"
REVIEW_PATH = "REVIEW.md"


def _slug(text: str, limit: int = 64) -> str:
    ascii_text = (text or "").encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug[:limit].strip("-") or "mvp-project"


def _frontmatter(name: str, description: str) -> str:
    safe_description = " ".join((description or f"Implementation agent for {name}").split())
    return f"---\nname: {name}\ndescription: {safe_description}\n---\n\n"


def bundle_file_plan(strategy: dict[str, Any] | None) -> list[BundleFileSpec]:
    specs = [
        BundleFileSpec(
            "README.md",
            README_SYSTEM,
            "Write the project README.md scaffold now.",
            "# MVP Project\n\nSee `PRD.md`, `ARCHITECTURE.md`, and `plan.md` to get started.\n",
            wave=2,
            needs=("PRD.md", "ARCHITECTURE.md"),
        ),
        BundleFileSpec(
            "PRD.md",
            PRD_SYSTEM,
            "Write PRD.md now with numbered requirements.",
            "# PRD\n\n- R1: Deliver the core MVP workflow end to end.\n",
        ),
        BundleFileSpec(
            "ARCHITECTURE.md",
            ARCHITECTURE_DOC_SYSTEM,
            "Write ARCHITECTURE.md now for the chosen option.",
            "# Architecture\n\nFollow the recommended architecture option from the discussion context.\n",
        ),
        BundleFileSpec(
            "AGENTS.md",
            ROOT_AGENTS_SYSTEM,
            "Write the root `AGENTS.md` now. Keep it sharp, directive, and ready for implementation agents.",
            (
                "# Project Operating Guide\n\n"
                "- Read `PRD.md`, `ARCHITECTURE.md`, and `plan.md` before starting any work.\n"
                "- Use stable data contracts and update them deliberately.\n"
                "- Keep changes scoped to one task at a time.\n"
                "- Add unit/integration tests and target 80% coverage for changed scope.\n"
            ),
            wave=2,
            needs=("PRD.md", "ARCHITECTURE.md"),
        ),
        BundleFileSpec(
            "contracts/AGENTS.md",
            CONTRACTS_AGENTS_SYSTEM,
            "Write `contracts/AGENTS.md` now. Make it the contract governance guide for parallel agents.",
            (
                "# Contract Governance\n\n"
                "- Every shared interface gets a stable contract ID.\n"
                "- Breaking changes require a migration note and downstream review.\n"
                "- Contract tests are mandatory for shared boundaries.\n"
            ),
            wave=2,
            needs=("ARCHITECTURE.md",),
        ),
        BundleFileSpec(
            "application/AGENTS.md",
            APPLICATION_AGENTS_SYSTEM,
            "Write `application/AGENTS.md` now. Optimize it for agents implementing one task at a time.",
            (
                "# Application Delivery Guide\n\n"
                "- Start from the assigned task in `plan.md`.\n"
                "- Consume only the listed contracts and update acceptance checks.\n"
                "- Add observability, tests, and handoff notes before closing the task.\n"
            ),
            wave=2,
            needs=("ARCHITECTURE.md",),
        ),
        BundleFileSpec(
            "quality/AGENTS.md",
            QUALITY_AGENTS_SYSTEM,
            "Write `quality/AGENTS.md` now. Make the testing and verification standard explicit.",
            (
                "# Quality Standard\n\n"
                "- Every task needs agent-run verification.\n"
                "- Add unit/integration tests and target at least 80% coverage for changed scope.\n"
                "- Keep one top-level test command green at all times.\n"
            ),
            wave=2,
            needs=("PRD.md",),
        ),
        BundleFileSpec(
            PLAN_PATH,
            PLAN_WRITER_SYSTEM,
            (
                "Write the execution plan now as structured data: the contract registry, then every task from "
                "MVP setup through launch readiness, each assigned to one of the execution-strategy workstreams, "
                "then the project commands."
            ),
            render_plan_markdown(fallback_plan(strategy, "")),
            wave=2,
            needs=("PRD.md", "ARCHITECTURE.md"),
            structured=True,
        ),
    ]
    workstreams = (strategy or {}).get("workstreams") or []
    for workstream in workstreams:
        name = str(workstream.get("name") or "").strip()
        if not name:
            continue
        focus = str(workstream.get("focus") or "").strip()
        deliverables = str(workstream.get("deliverables") or "").strip()
        specs.append(
            BundleFileSpec(
                f".claude/agents/{name}.md",
                SUBAGENT_SYSTEM,
                (
                    f"Write the subagent definition body for workstream `{name}` now. "
                    f"Workstream focus: {focus or 'general implementation'}. "
                    f"Deliverables: {deliverables or 'working, tested code'}."
                ),
                (
                    f"You are the `{name}` implementation agent. Read `AGENTS.md`, `PRD.md`, `ARCHITECTURE.md`, "
                    f"and `plan.md`, then implement the plan tasks assigned to the `{name}` workstream with "
                    "tests, keeping the project test suite green."
                ),
                frontmatter=_frontmatter(name, focus),
                wave=3,
                needs=("plan.md",),
            )
        )
    return specs


def truncate_document(text: str, limit: int) -> str:
    """Cut `text` to about `limit` characters at a line break, marking that something is missing."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    line_break = cut.rfind("\n")
    if line_break > limit * 0.8:
        cut = cut[:line_break]
    return f"{cut.rstrip()}\n\n{TRUNCATED_MARKER}"


def prompt_for(
    spec: BundleFileSpec,
    context_block: str,
    generated: dict[str, str],
    *,
    issues: str = "",
    previous: str = "",
) -> str:
    """The user prompt for one document: the shared context, the upstream documents it needs (when
    they exist and are not empty), for a revision the reviewer's issues and the previous version, then
    the instruction last."""
    parts = [context_block]
    for path in spec.needs:
        text = (generated.get(path) or "").strip()
        if text:
            parts.append(f"## Upstream document: {path}\n\n{truncate_document(text, MAX_UPSTREAM_CHARS)}")
    if issues.strip():
        parts.append(f"## Review issues to fix in this revision\n\n{issues.strip()}")
    if previous.strip():
        parts.append(
            f"## Your previous version of {spec.relative_path}\n\n{truncate_document(previous.strip(), MAX_UPSTREAM_CHARS)}"
        )
    parts.append(spec.instruction)
    return "\n\n".join(parts)


def write_bundle(
    *,
    root: Path,
    user_idea: str,
    docs: dict[str, str],
    strategy: dict[str, Any] | None,
    review_issues: Sequence[str] = (),
) -> tuple[Path, list[str], str]:
    """Write the generated documents (an empty or missing one gets its fallback), `plan.json` with the
    `plan.md` rendered from it, `REVIEW.md` when the critic left notes, and STRATEGY.json into a fresh
    timestamped folder under `root`."""
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    bundle_dir = Path(root) / f"{timestamp}-{_slug(user_idea)}"
    created: list[str] = []

    def write(relative_path: str, content: str) -> None:
        target = bundle_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content.rstrip("\n") + "\n", encoding="utf-8")
        created.append(relative_path)

    for spec in bundle_file_plan(strategy):
        if spec.relative_path == PLAN_PATH:
            # plan.json is the source of truth; a missing or broken one becomes a valid fallback plan.
            plan = load_plan(docs.get(PLAN_JSON_PATH)) or fallback_plan(strategy, docs.get("PRD.md") or "")
            write(PLAN_PATH, render_plan_markdown(plan))
            write(PLAN_JSON_PATH, plan.model_dump_json(indent=2))
            continue
        content = (docs.get(spec.relative_path) or "").strip() or spec.fallback.strip()
        write(spec.relative_path, spec.frontmatter + content)

    if (docs.get(REVIEW_PATH) or "").strip():
        write(REVIEW_PATH, docs[REVIEW_PATH].strip())
    write("STRATEGY.json", json.dumps(strategy or {}, indent=2))

    summary_lines = [
        f"Generated an agent-ready project pack in `{bundle_dir}`.",
        "",
        "Created files:",
        *[f"- `{path}`" for path in created],
        "",
        "Start with `PRD.md` and `plan.md` (`plan.json` is the machine-readable plan); `.claude/agents/` holds "
        "the implementation subagent definitions and `STRATEGY.json` records the chosen execution mode.",
    ]
    if review_issues:
        summary_lines += [
            "",
            f"**{len(review_issues)} open review note(s)** are listed in `{REVIEW_PATH}`:",
            *[f"- {issue}" for issue in list(review_issues)[:5]],
        ]
    return bundle_dir, created, "\n".join(summary_lines)


def build_bundle_context(
    *,
    user_idea: str,
    summary: str,
    questions: list[str],
    user_answers: str,
    architecture: str,
    arch_choice: dict[str, Any] | None,
    strategy: dict[str, Any] | None,
    planning_request: str,
    chosen_option_details: str = "",
) -> str:
    choice = arch_choice or {}
    extra_guidance = (planning_request or "").strip()
    strategy_text = json.dumps(strategy, indent=2) if strategy else "No execution strategy available."
    details = chosen_option_details.strip()
    details_block = f"Chosen option details:\n{details}\n\n" if details else ""
    return (
        f"Original idea:\n{user_idea.strip()}\n\n"
        f"Discussion summary:\n{summary.strip() or 'No summary available.'}\n\n"
        f"Questions asked:\n{chr(10).join(questions) or 'No explicit questions.'}\n\n"
        f"User answers:\n{user_answers.strip() or 'No user answers provided.'}\n\n"
        f"Architecture options:\n{architecture.strip() or 'No architecture provided.'}\n\n"
        f"Chosen architecture option: {choice.get('option') or 'A'}"
        f" (user notes: {choice.get('notes') or 'none'})\n\n"
        f"{details_block}"
        f"Execution strategy:\n{strategy_text}\n\n"
        f"Extra planning guidance from user:\n{extra_guidance or 'No extra guidance beyond generating the execution pack.'}"
    )

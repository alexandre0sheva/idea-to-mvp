"""Blueprint v2 generation: the agent-ready project pack written before implementation.

This module owns the document prompts and file layout of a project bundle. It does
not call any LLM itself: callers inject ``generate_doc(system_prompt, user_prompt)``
so the module stays import-cycle-free and trivially testable.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

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
    "Number every requirement (R1, R2, ...) so plan tasks and tests can reference them.\n"
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

PLAN_SYSTEM = (
    "You are creating an execution-ready `plan.md` for a greenfield MVP project.\n"
    "The plan will be consumed by autonomous coding agents, one task at a time.\n"
    "Produce a complete start-to-finish MVP task plan with sequencing, dependencies, data contracts, and tests.\n"
    "Requirements:\n"
    "- include all major tasks needed to reach an MVP release, not just engineering implementation\n"
    "- each task is owned by one workstream (use the workstream names from the execution strategy)\n"
    "- do not collapse unrelated work into giant tasks\n"
    "- if one task can affect another, reference the shared data contract IDs explicitly\n"
    "- reference PRD requirement IDs (R1, R2, ...) in each task's acceptance criteria\n"
    "- keep `plan.md` task-only: no project overview, no repeated AGENTS guidance\n"
    "- start with a compact contract registry near the top with stable IDs like `C1`, `C2`, ...\n"
    "- after the contract registry, output only a numbered task list in execution order\n"
    "- each task must include exactly these fields: Goal, Workstream, Depends on, Contracts in, Contracts out, "
    "Implementation scope, Acceptance criteria, Required unit/integration tests, Coverage target, "
    "Handoff artifacts\n"
    "- keep the writing dense, structured, and implementation-oriented"
)

SUBAGENT_SYSTEM = (
    "You are writing the body of a Claude Code subagent definition for one implementation workstream.\n"
    "Write a focused system prompt (no YAML frontmatter - it is added separately) that tells this agent:\n"
    "its workstream mission and deliverables, which plan.md tasks belong to it, which docs to read first "
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
        ),
        BundleFileSpec(
            "plan.md",
            PLAN_SYSTEM,
            (
                "Write `plan.md` now. Use markdown only. Start with `## Contract Registry`, then `## Tasks`, "
                "then a numbered task list from MVP setup through launch readiness, assigning every task to one "
                "of the execution-strategy workstreams."
            ),
            (
                "# MVP Plan\n\n"
                "## Contract Registry\n\n- C1: Core API contract\n\n"
                "## Tasks\n\n"
                "### 01. Project setup\n"
                "- Goal: establish the MVP baseline.\n"
                "- Workstream: core-product.\n"
                "- Depends on: none.\n"
                "- Contracts in: none.\n"
                "- Contracts out: C1.\n"
                "- Implementation scope: repository scaffold, tooling, CI, local run path.\n"
                "- Acceptance criteria: install, lint, test, and app boot commands work.\n"
                "- Required unit/integration tests: smoke coverage for config and startup paths.\n"
                "- Coverage target: at least 80% for changed scope.\n"
                "- Handoff artifacts: updated setup files and task notes.\n"
            ),
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
            )
        )
    return specs


def create_project_bundle(
    *,
    root: Path,
    user_idea: str,
    context_block: str,
    strategy: dict[str, Any] | None,
    generate_doc: Callable[[str, str], str],
) -> tuple[Path, list[str], str]:
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
    bundle_dir = Path(root) / f"{timestamp}-{_slug(user_idea)}"
    created: list[str] = []

    for spec in bundle_file_plan(strategy):
        target = bundle_dir / spec.relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        user_prompt = f"{context_block}\n\n{spec.instruction}"
        content = (generate_doc(spec.system_prompt, user_prompt) or "").strip() or spec.fallback.strip()
        target.write_text(spec.frontmatter + content + "\n", encoding="utf-8")
        created.append(spec.relative_path)

    strategy_path = bundle_dir / "STRATEGY.json"
    strategy_path.write_text(json.dumps(strategy or {}, indent=2) + "\n", encoding="utf-8")
    created.append("STRATEGY.json")

    summary_lines = [
        f"Generated an agent-ready project pack in `{bundle_dir}`.",
        "",
        "Created files:",
        *[f"- `{path}`" for path in created],
        "",
        "Start with `PRD.md` and `plan.md`; `.claude/agents/` holds the implementation subagent definitions "
        "and `STRATEGY.json` records the chosen execution mode.",
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
) -> str:
    choice = arch_choice or {}
    extra_guidance = (planning_request or "").strip()
    strategy_text = json.dumps(strategy, indent=2) if strategy else "No execution strategy available."
    return (
        f"Original idea:\n{user_idea.strip()}\n\n"
        f"Discussion summary:\n{summary.strip() or 'No summary available.'}\n\n"
        f"Questions asked:\n{chr(10).join(questions) or 'No explicit questions.'}\n\n"
        f"User answers:\n{user_answers.strip() or 'No user answers provided.'}\n\n"
        f"Architecture options:\n{architecture.strip() or 'No architecture provided.'}\n\n"
        f"Chosen architecture option: {choice.get('option') or 'A'}"
        f" (user notes: {choice.get('notes') or 'none'})\n\n"
        f"Execution strategy:\n{strategy_text}\n\n"
        f"Extra planning guidance from user:\n{extra_guidance or 'No extra guidance beyond generating the execution pack.'}"
    )

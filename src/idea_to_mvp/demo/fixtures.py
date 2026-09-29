"""Canned responses for demo mode, keyed by pipeline role.

Every response echoes a slice of the user's idea so the demo feels real, and follows the output
contract of the real prompt (section headings, numbered questions, strict JSON for strategy).
Adding an LLM call to the pipeline means adding a fixture here (see docs/architecture.md).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from langchain_core.messages import BaseMessage
from pydantic import BaseModel

from idea_to_mvp import blueprints
from idea_to_mvp.schemas import (
    ArchitectureOption,
    ArchitectureProposal,
    ExecutionStrategy,
    MvpQuestion,
    QuestionSet,
    Workstream,
)
from idea_to_mvp.text_utils import normalize_content

_IDEA_RE = re.compile(
    r"(?:Anchor idea|Initial user idea|Original idea|Initial idea|Idea):\n(.+?)(?:\n\n|\Z)", flags=re.DOTALL
)
_WORKSTREAM_RE = re.compile(r'"name":\s*"([a-z0-9-]+)"')
_ROUND_RE = re.compile(r"round (\d+) of (\d+)")
_DEFAULT_WORKSTREAMS = ["core-app", "quality"]

STRATEGY_REASONING = (
    "Two loosely coupled workstreams: the product code and a quality lane owning tests and fixtures. "
    "A sequential agent team keeps ownership clear."
)


def _system_text(messages: list[BaseMessage]) -> str:
    return normalize_content(messages[0].content) if messages else ""


def _human_text(messages: list[BaseMessage]) -> str:
    return "\n\n".join(normalize_content(m.content) for m in messages[1:])


def idea_from(messages: list[BaseMessage]) -> str:
    match = _IDEA_RE.search(_human_text(messages))
    idea = " ".join(match.group(1).split()) if match else "your product idea"
    return idea[:110].rstrip(" .")


def workstreams_from(messages: list[BaseMessage]) -> list[str]:
    names = _WORKSTREAM_RE.findall(_human_text(messages))
    return list(dict.fromkeys(names)) or list(_DEFAULT_WORKSTREAMS)


# ------------------------------------------------------------------ panel


def _round_note(messages: list[BaseMessage]) -> str:
    text = _human_text(messages)
    match = _ROUND_RE.search(text)
    if match and match.group(1) == match.group(2):
        return "Final round: I am converging on the smallest slice worth building and cutting the rest."
    return "I will push one open question forward this round."


def pm(messages: list[BaseMessage]) -> str:
    idea = idea_from(messages)
    return (
        f"**Scope for “{idea}”**\n"
        "- Tech Lead is right that the first release should serve one user journey end to end.\n"
        "- The Skeptic's adoption worry is fair: the MVP must show value in the first session.\n"
        f"- {_round_note(messages)}\n\n"
        "**Functionality recommendation**\n"
        "Ship capture, review, and a simple progress view. Defer sharing and integrations.\n\n"
        "**Implementation-scoped next test**\n"
        "Have five target users complete the core journey unaided and record where they stall."
    )


def tech_lead(messages: list[BaseMessage]) -> str:
    idea = idea_from(messages)
    return (
        f"**Feasibility of “{idea}”**\n"
        "- PM's scope is buildable as a small web app with one relational store.\n"
        "- The Skeptic's data-loss concern is cheap to address with autosave and export.\n"
        f"- {_round_note(messages)}\n\n"
        "**Tech / build notes**\n"
        "A monolith with a thin API layer and server-rendered UI; avoid queues until load demands them.\n\n"
        "**Validation steps**\n"
        "Load-test the core write path and verify export round-trips."
    )


def skeptic(messages: list[BaseMessage]) -> str:
    idea = idea_from(messages)
    return (
        f"**Risks in “{idea}”**\n"
        "- PM assumes users will change habits for this; that is unproven.\n"
        "- Tech Lead's monolith is fine, but privacy expectations could change the storage design.\n"
        f"- {_round_note(messages)}\n\n"
        "**Pushback on functionality/implementation**\n"
        "Cut everything not needed for the core journey, and test demand with a landing page first.\n\n"
        "**Evidence needed next**\n"
        "Ten interviews and one paid pilot commitment before building beyond the MVP."
    )


# ---------------------------------------------------------- summarizer


def _summary(messages: list[BaseMessage]) -> str:
    idea = idea_from(messages)
    return (
        "## Executive summary\n"
        f"The panel converged on a focused first release of “{idea}”: one core user journey, a small "
        "monolithic implementation, and validation with real users before expanding scope.\n\n"
        "## Functional specification snapshot\n- Core journey end to end\n- Simple progress view\n- Export\n\n"
        "## Technical implementation snapshot\n- Web monolith with one relational store\n- Autosave and export\n\n"
        "## Disagreements and resolutions\n- Scope vs. speed: resolved by deferring sharing and integrations.\n"
        "- Storage privacy: open, decide with the user.\n\n"
        "## Refined idea\n- Narrower, journey-first product.\n\n"
        "## SWOT snapshot\n- Strengths: focus\n- Weaknesses: unproven demand\n"
        "- Opportunities: underserved niche\n- Threats: incumbents\n\n"
        "## Business model notes\n- Start free; test willingness to pay after activation.\n\n"
        "## Risks & mitigations\n- Weak demand: interviews first\n- Data loss: autosave\n\n"
        "## Recommended next steps\n- Answer the MVP questions\n- Choose an architecture\n- Generate the blueprint\n"
    )


def summarizer(messages: list[BaseMessage]) -> str:
    return _summary(messages)


# ------------------------------------------------------- architect docs


def _readme_doc(messages: list[BaseMessage]) -> str:
    idea = idea_from(messages)
    return (
        f"# {idea[:60]}\n\nMVP of “{idea}”.\n\n## Features\n- Core journey\n- Progress view\n\n"
        "## Setup\nSee `ARCHITECTURE.md` for the chosen stack.\n\n## Test\nRun the project's test command.\n"
    )


def _prd_doc(messages: list[BaseMessage]) -> str:
    idea = idea_from(messages)
    return (
        f"# PRD: {idea}\n\n## Problem\nUsers lack a focused tool for this job.\n\n"
        "## Requirements\n- R1 (P0): The user can complete the core journey end to end.\n"
        "- R2 (P0): Progress is visible after each session.\n- R3 (P1): Data can be exported.\n\n"
        "## Non-goals\n- Sharing and third-party integrations.\n\n## Success metrics\n- 5 of 5 pilot users finish the journey.\n"
    )


def _architecture_doc(messages: list[BaseMessage]) -> str:
    return (
        "# Architecture\n\n## Overview\nModular monolith with server-rendered UI and one relational store.\n\n"
        "```\nbrowser -> web app -> storage\n```\n\n## Components\n- web: routes and templates\n"
        "- domain: core logic\n- storage: persistence\n\n## API surface\n- `GET /` overview\n- `POST /entries` create entry\n"
    )


def _agents_doc(title: str) -> Callable[[list[BaseMessage]], str]:
    def build(messages: list[BaseMessage]) -> str:
        return (
            f"# {title}\n\n- Read `PRD.md`, `ARCHITECTURE.md`, and `plan.md` first.\n"
            "- Keep changes scoped to one task and keep the test suite green.\n"
            "- Add tests for every task; hand off with notes in `plan.md`.\n"
        )

    return build


def _plan_doc(messages: list[BaseMessage]) -> str:
    streams = workstreams_from(messages)
    tasks = []
    titles = ["Project setup", "Core journey", "Progress view"]
    for index, title in enumerate(titles, start=1):
        stream = streams[(index - 1) % len(streams)]
        tasks.append(
            f"### {index:02d}. {title}\n"
            f"- Goal: deliver {title.lower()}.\n- Workstream: {stream}.\n"
            f"- Depends on: {'none' if index == 1 else f'{index - 1:02d}'}.\n"
            f"- Contracts in: {'none' if index == 1 else 'C1'}.\n- Contracts out: C1.\n"
            "- Implementation scope: code and tests for this slice.\n"
            f"- Acceptance criteria: R{min(index, 3)} is demonstrably satisfied.\n"
            "- Required unit/integration tests: one per acceptance criterion.\n"
            "- Coverage target: at least 80% for changed scope.\n- Handoff artifacts: updated docs and notes.\n"
        )
    return "## Contract Registry\n\n- C1: Core entry contract\n\n## Tasks\n\n" + "\n".join(tasks)


def _subagent_doc(messages: list[BaseMessage]) -> str:
    match = re.search(r"workstream `([a-z0-9-]+)`", _human_text(messages))
    name = match.group(1) if match else "workstream"
    return (
        f"You are the `{name}` implementation agent. Read `AGENTS.md`, `PRD.md`, `ARCHITECTURE.md`, and "
        f"`plan.md`, then implement every task assigned to the `{name}` workstream with tests. "
        "Respect the contracts, keep the suite green, and record hand-off notes in `plan.md`."
    )


_ARCHITECT_BY_SYSTEM: dict[str, Callable[[list[BaseMessage]], str]] = {
    blueprints.README_SYSTEM: _readme_doc,
    blueprints.PRD_SYSTEM: _prd_doc,
    blueprints.ARCHITECTURE_DOC_SYSTEM: _architecture_doc,
    blueprints.ROOT_AGENTS_SYSTEM: _agents_doc("Project Operating Guide (demo)"),
    blueprints.CONTRACTS_AGENTS_SYSTEM: _agents_doc("Contract Governance (demo)"),
    blueprints.APPLICATION_AGENTS_SYSTEM: _agents_doc("Application Delivery Guide (demo)"),
    blueprints.QUALITY_AGENTS_SYSTEM: _agents_doc("Quality Standard (demo)"),
    blueprints.PLAN_SYSTEM: _plan_doc,
    blueprints.SUBAGENT_SYSTEM: _subagent_doc,
}


def architect(messages: list[BaseMessage]) -> str:
    """Text documents written by the architect runtime (the blueprint pack); told apart by system prompt."""
    builder = _ARCHITECT_BY_SYSTEM.get(_system_text(messages))
    if builder is None:
        raise KeyError("No demo fixture for this architect prompt; add one to demo/fixtures.py")
    return builder(messages)


# ------------------------------------------------- structured outputs


def question_set(messages: list[BaseMessage]) -> QuestionSet:
    idea = idea_from(messages)
    rows = [
        ("Which single user journey must work end to end in the MVP?",
         "It fixes the scope every other decision hangs on.",
         f"One core journey for “{idea[:60]}”, from first use to the first valuable result."),
        ("Which capabilities are explicitly out of scope for v0?",
         "It prevents building features nobody validated.",
         "Sharing, integrations, and admin tooling wait until after validation."),
        ("What data must be stored, and what are the privacy expectations?",
         "It drives the persistence and security choices.",
         "Per-user records in one relational store; no data sold or shared."),
        ("Which integrations, if any, are mandatory at launch?",
         "Each integration multiplies build and test effort.",
         "None; export to a file is enough for launch."),
        ("What result would prove the MVP worked within its first month?",
         "It defines what the first version must measure.",
         "Most pilot users complete the core journey at least twice in a week."),
    ]
    return QuestionSet(
        questions=[MvpQuestion(question=q, why_it_matters=w, suggested_answer=a) for q, w, a in rows]
    )


def architecture_proposal(messages: list[BaseMessage]) -> ArchitectureProposal:
    idea = idea_from(messages)
    return ArchitectureProposal(
        option_a=ArchitectureOption(
            key="A",
            name="Modular monolith",
            style=f"Server-rendered modular monolith for “{idea[:60]}”",
            stack=["Python", "FastAPI", "HTMX"],
            persistence="SQLite with migrations",
            integrations=["None at launch"],
            security_baseline="Session auth, input validation, nightly backups",
            tradeoffs=["Single-node ceiling"],
            limits="Hundreds of concurrent users",
            anchored_constraints=["Small team", "Fast launch"],
        ),
        option_b=ArchitectureOption(
            key="B",
            name="API + SPA with workers",
            style="Separate API, single-page app, and background workers",
            stack=["TypeScript", "React", "Postgres", "Redis"],
            persistence="Managed Postgres plus cache",
            integrations=["Queue", "Object storage"],
            security_baseline="OAuth, rate limiting, audit logs",
            tradeoffs=["More moving parts to run"],
            limits="Scales horizontally to very high load",
            anchored_constraints=["Small team", "Growth expected"],
        ),
        shared_components=["Domain model", "Export format"],
        recommendation="A",
        recommendation_rationale="It ships fastest and the hot paths can be extracted later.",
        biggest_tradeoff="Single-node ceiling until workers are extracted",
        rollout=["MVP on the monolith", "Extract workers when load demands"],
    )


def execution_strategy(messages: list[BaseMessage]) -> ExecutionStrategy:
    return ExecutionStrategy(
        mode="agent_team",
        reasoning=STRATEGY_REASONING,
        workstreams=[
            Workstream(
                name="core-app",
                focus="Implement the product journey end to end.",
                deliverables="Working application code.",
            ),
            Workstream(
                name="quality",
                focus="Tests, fixtures, and verification for every task.",
                deliverables="A green automated test suite.",
            ),
        ],
    )


RESPONSES: dict[str, Callable[[list[BaseMessage]], str]] = {
    "pm": pm,
    "tech_lead": tech_lead,
    "skeptic": skeptic,
    "summarizer": summarizer,
    "architect": architect,
}

STRUCTURED: dict[type[BaseModel], Callable[[list[BaseMessage]], Any]] = {
    QuestionSet: question_set,
    ArchitectureProposal: architecture_proposal,
    ExecutionStrategy: execution_strategy,
}


def respond(role: str, messages: list[BaseMessage]) -> str:
    try:
        builder = RESPONSES[role]
    except KeyError as exc:
        raise KeyError(f"No demo text fixture for role {role!r}; add one to demo/fixtures.py") from exc
    return builder(messages)


def respond_structured(schema: type[BaseModel], messages: list[BaseMessage]) -> BaseModel:
    try:
        builder = STRUCTURED[schema]
    except KeyError as exc:
        raise KeyError(f"No demo fixture for schema {schema.__name__}; add one to demo/fixtures.py") from exc
    return builder(messages)

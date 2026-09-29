"""Typed outputs exchanged between pipeline agents.

Schema design rules (they keep the schemas usable with every provider's strict structured-output
mode): every field is required (no defaults), and count/length limits are enforced by Python
validators rather than JSON-schema keywords (`minItems`, `maxLength`, ...), which some providers
reject. The LLM layer retries once with the validation error, so a validator failure costs one call.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, field_validator, model_validator

MAX_WORKSTREAMS = 5
QUESTION_COUNT = 5


# ---------------------------------------------------------------- questions


class MvpQuestion(BaseModel):
    question: str
    why_it_matters: str
    suggested_answer: str  # used by autopilot and the answers form


class QuestionSet(BaseModel):
    questions: list[MvpQuestion]

    @field_validator("questions")
    @classmethod
    def _exactly_five(cls, value: list[MvpQuestion]) -> list[MvpQuestion]:
        if len(value) != QUESTION_COUNT:
            raise ValueError(f"exactly {QUESTION_COUNT} questions are required, got {len(value)}")
        return value


def render_questions(question_set: QuestionSet) -> list[str]:
    return [f"{i}. {q.question.strip()}" for i, q in enumerate(question_set.questions, start=1)]


# ------------------------------------------------------------- architecture


class ArchitectureOption(BaseModel):
    key: Literal["A", "B"]
    name: str
    style: str
    stack: list[str]
    persistence: str
    integrations: list[str]
    security_baseline: str
    tradeoffs: list[str]
    limits: str
    anchored_constraints: list[str]  # constraints from the user's answers this option is built around


class ArchitectureProposal(BaseModel):
    option_a: ArchitectureOption
    option_b: ArchitectureOption
    shared_components: list[str]
    recommendation: Literal["A", "B"]
    recommendation_rationale: str
    biggest_tradeoff: str
    rollout: list[str]  # MVP phase -> scale-up phase

    @model_validator(mode="after")
    def _keys_follow_position(self) -> ArchitectureProposal:
        self.option_a.key = "A"
        self.option_b.key = "B"
        return self

    def option(self, key: str) -> ArchitectureOption:
        return self.option_b if key.strip().upper() == "B" else self.option_a


def _bullets(items: list[str]) -> str:
    return "\n".join(f"  - {item}" for item in items) if items else "  - (none)"


def render_option_markdown(proposal: ArchitectureProposal, key: str) -> str:
    option = proposal.option(key)
    return (
        f"## Option {option.key} - {option.name}\n"
        f"- Architecture style: {option.style}\n"
        f"- Stack:\n{_bullets(option.stack)}\n"
        f"- Persistence: {option.persistence}\n"
        f"- Integrations:\n{_bullets(option.integrations)}\n"
        f"- Security/reliability baseline: {option.security_baseline}\n"
        f"- Tradeoffs:\n{_bullets(option.tradeoffs)}\n"
        f"- Limits: {option.limits}\n"
        f"- Anchored to your constraints:\n{_bullets(option.anchored_constraints)}\n"
    )


def render_architecture_markdown(proposal: ArchitectureProposal) -> str:
    shared = "\n".join(f"- {item}" for item in proposal.shared_components) or "- (none)"
    rollout = "\n".join(f"{i}. {step}" for i, step in enumerate(proposal.rollout, start=1)) or "- (none)"
    return (
        f"{render_option_markdown(proposal, 'A')}\n"
        f"{render_option_markdown(proposal, 'B')}\n"
        f"## Shared components\n{shared}\n\n"
        f"## Architect recommendation\n"
        f"- Recommended: Option {proposal.recommendation}\n"
        f"- Why: {proposal.recommendation_rationale}\n"
        f"- Biggest tradeoff accepted: {proposal.biggest_tradeoff}\n"
        f"- Phased rollout:\n{rollout}\n"
    )


# ----------------------------------------------------------------- strategy


class Workstream(BaseModel):
    name: str
    focus: str
    deliverables: str

    @field_validator("name")
    @classmethod
    def _kebab_case(cls, value: str) -> str:
        slug = re.sub(r"[^a-z0-9-]+", "-", value.strip().lower()).strip("-")
        if not slug:
            raise ValueError("workstream name must contain letters or digits")
        return slug


class ExecutionStrategy(BaseModel):
    mode: Literal["subagents", "agent_team"]
    reasoning: str
    workstreams: list[Workstream]

    @field_validator("workstreams", mode="before")
    @classmethod
    def _clean_workstreams(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        usable = [
            item
            for item in value
            if (isinstance(item, dict) and str(item.get("name") or "").strip()) or isinstance(item, Workstream)
        ]
        return usable[:MAX_WORKSTREAMS]

    @field_validator("workstreams")
    @classmethod
    def _at_least_one(cls, value: list[Workstream]) -> list[Workstream]:
        if not value:
            raise ValueError("at least one workstream is required")
        return value

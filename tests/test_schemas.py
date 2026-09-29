import json

import pytest
from pydantic import ValidationError

from idea_to_mvp.schemas import (
    ArchitectureOption,
    ArchitectureProposal,
    ExecutionStrategy,
    MvpQuestion,
    QuestionSet,
    Workstream,
    render_architecture_markdown,
    render_option_markdown,
    render_questions,
)


def _question(n: int = 1) -> MvpQuestion:
    return MvpQuestion(question=f"Question {n}?", why_it_matters="It changes scope.", suggested_answer="Do X.")


def _option(key: str = "A", name: str = "Monolith") -> ArchitectureOption:
    return ArchitectureOption(
        key=key,
        name=name,
        style="Modular monolith",
        stack=["Python", "FastAPI"],
        persistence="SQLite",
        integrations=["none"],
        security_baseline="Session auth",
        tradeoffs=["Single node"],
        limits="Hundreds of users",
        anchored_constraints=["Solo founder", "Ship in 2 weeks"],
    )


def _proposal(recommendation: str = "A") -> ArchitectureProposal:
    return ArchitectureProposal(
        option_a=_option("A", "Fast Monolith"),
        option_b=_option("B", "Scalable Services"),
        shared_components=["Domain model"],
        recommendation=recommendation,
        recommendation_rationale="Launch speed.",
        biggest_tradeoff="Single-node ceiling",
        rollout=["MVP on A", "Extract workers"],
    )


def test_question_set_requires_exactly_five_questions() -> None:
    assert len(QuestionSet(questions=[_question(i) for i in range(5)]).questions) == 5
    with pytest.raises(ValidationError, match="exactly 5"):
        QuestionSet(questions=[_question(i) for i in range(4)])
    with pytest.raises(ValidationError, match="exactly 5"):
        QuestionSet(questions=[_question(i) for i in range(6)])


def test_render_questions_numbers_each_question() -> None:
    lines = render_questions(QuestionSet(questions=[_question(i) for i in range(1, 6)]))
    assert lines[0] == "1. Question 1?"
    assert lines[4] == "5. Question 5?"


def test_workstream_names_are_slugified() -> None:
    assert Workstream(name=" Web UI! ", focus="f", deliverables="d").name == "web-ui"
    with pytest.raises(ValidationError):
        Workstream(name="!!!", focus="f", deliverables="d")


def test_strategy_drops_nameless_workstreams_and_caps_at_five() -> None:
    raw = {
        "mode": "subagents",
        "reasoning": "r",
        "workstreams": [{"name": "", "focus": "x", "deliverables": "y"}, "junk"]
        + [{"name": f"ws-{i}", "focus": "f", "deliverables": "d"} for i in range(7)],
    }
    strategy = ExecutionStrategy.model_validate(raw)
    assert [w.name for w in strategy.workstreams] == [f"ws-{i}" for i in range(5)]


def test_strategy_rejects_unknown_mode_and_empty_workstreams() -> None:
    with pytest.raises(ValidationError):
        ExecutionStrategy.model_validate({"mode": "swarm", "reasoning": "r", "workstreams": [
            {"name": "a", "focus": "f", "deliverables": "d"}]})
    with pytest.raises(ValidationError):
        ExecutionStrategy.model_validate({"mode": "subagents", "reasoning": "r", "workstreams": []})


def test_proposal_normalizes_option_keys_by_position() -> None:
    proposal = ArchitectureProposal(
        option_a=_option("B", "First"),
        option_b=_option("A", "Second"),
        shared_components=[],
        recommendation="A",
        recommendation_rationale="r",
        biggest_tradeoff="t",
        rollout=[],
    )
    assert proposal.option_a.key == "A" and proposal.option_b.key == "B"


def test_proposal_recommendation_must_be_a_or_b() -> None:
    with pytest.raises(ValidationError):
        _proposal(recommendation="C")


def test_render_architecture_markdown_names_both_options_and_the_recommendation() -> None:
    text = render_architecture_markdown(_proposal("B"))
    assert "## Option A" in text and "Fast Monolith" in text
    assert "## Option B" in text and "Scalable Services" in text
    assert "Architect recommendation" in text and "Option B" in text.split("Architect recommendation")[1]
    assert "Solo founder" in text


def test_render_option_markdown_returns_only_the_chosen_option() -> None:
    chosen = render_option_markdown(_proposal(), "B")
    assert "Scalable Services" in chosen and "Fast Monolith" not in chosen


def test_json_schemas_avoid_constraints_providers_reject_in_strict_modes() -> None:
    for model in (QuestionSet, ArchitectureProposal, ExecutionStrategy):
        blob = json.dumps(model.model_json_schema())
        for keyword in ("maxLength", "minLength", "maxItems", "minItems"):
            assert keyword not in blob, f"{model.__name__} schema uses {keyword}"

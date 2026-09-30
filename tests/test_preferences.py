"""Project preferences: the typed model, the prompt block, and that every stage's prompt carries it."""

from typing import Any

import pytest
from langchain_core.messages import BaseMessage
from pydantic import ValidationError

from idea_to_mvp import llm
from idea_to_mvp.blueprints import bundle_file_plan
from idea_to_mvp.nodes.architecture import architect_node
from idea_to_mvp.nodes.blueprint import DocInput, blueprint_context, generate_doc_node
from idea_to_mvp.nodes.common import preferences_block
from idea_to_mvp.nodes.discussion import discussion_node
from idea_to_mvp.nodes.panel import OpeningInput, opening_turn_node
from idea_to_mvp.nodes.strategy import strategy_node
from idea_to_mvp.nodes.summary import summarizer_node
from idea_to_mvp.schemas import ProjectPreferences
from idea_to_mvp.state import make_initial_state

STATED = {
    "platform": "web",
    "stack_hints": "TypeScript + Postgres",
    "deploy_target": "Fly.io",
    "must_use": "Stripe for payments",
    "must_avoid": "MongoDB",
}


# ------------------------------------------------------------------- the model


def test_preferences_default_to_no_constraints() -> None:
    prefs = ProjectPreferences()
    assert prefs.platform == "any"
    assert (prefs.stack_hints, prefs.deploy_target, prefs.must_use, prefs.must_avoid) == ("", "", "", "")


def test_preferences_reject_an_unknown_platform() -> None:
    with pytest.raises(ValidationError):
        ProjectPreferences(platform="smart-fridge")  # type: ignore[arg-type]


def test_a_new_run_starts_with_the_preferences_it_was_given() -> None:
    assert make_initial_state("idea", 1)["preferences"] == ProjectPreferences().model_dump()
    state = make_initial_state("idea", 1, preferences=ProjectPreferences(**STATED))
    assert state["preferences"] == STATED


# ----------------------------------------------------------------- the block


@pytest.mark.parametrize("prefs", [None, {}, ProjectPreferences().model_dump(), {"stack_hints": "  ", "must_use": "\n"}])
def test_the_block_is_empty_when_nothing_was_stated(prefs: dict | None) -> None:
    assert preferences_block(prefs) == ""


def test_the_block_states_what_the_user_asked_for() -> None:
    block = preferences_block(STATED)
    for expected in ("web", "TypeScript + Postgres", "Fly.io", "Stripe for payments", "MongoDB"):
        assert expected in block


def test_must_use_and_must_avoid_are_hard_constraints() -> None:
    block = preferences_block(STATED)
    assert "hard constraint" in block.lower()
    assert "must use" in block.lower() and "must avoid" in block.lower()


def test_the_block_leaves_out_what_was_not_stated() -> None:
    block = preferences_block({"stack_hints": "Rust"})
    assert "Rust" in block and "Deploy" not in block and "Platform" not in block and "hard constraint" not in block.lower()


def test_the_block_ignores_unknown_keys_and_bad_values() -> None:
    assert "Rust" in preferences_block({"stack_hints": "Rust", "nonsense": 1})
    assert preferences_block({"platform": "smart-fridge"}) == ""  # an invalid value is dropped, not a crash


# ------------------------------------------------ the prompts carry the block


class FakeRuntime:
    def __init__(self, role: str = "") -> None:
        self.llm = object()
        self.provider = "anthropic"
        self.model = "fake"
        self.max_tokens = 256
        self.system_prompt = f"system::{role}"


@pytest.fixture()
def prompts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every prompt a node sends: the model calls are faked and return the node's own fallback."""
    sent: list[str] = []

    def text(runtime: Any, messages: list[BaseMessage], **_kw: Any) -> str:
        sent.append("\n".join(str(m.content) for m in messages[1:]))
        return "a document"

    def structured(runtime: Any, messages: list[BaseMessage], schema: Any, *, fallback: Any = None) -> Any:
        sent.append("\n".join(str(m.content) for m in messages[1:]))
        return fallback()

    monkeypatch.setattr(llm, "get_runtime", FakeRuntime)
    monkeypatch.setattr(llm, "invoke_text", text)
    monkeypatch.setattr(llm, "invoke_structured", structured)
    return sent


def stated_state() -> dict[str, Any]:
    state: dict[str, Any] = dict(make_initial_state("A climbing log", 1, preferences=ProjectPreferences(**STATED)))
    state.update(summary="s", generated_questions=["1. Q?"], user_answers="a", architecture="## Option A")
    return state


def assert_hard_constraints(prompt: str) -> None:
    assert "TypeScript + Postgres" in prompt and "Fly.io" in prompt
    assert "Stripe for payments" in prompt and "MongoDB" in prompt
    assert "hard constraint" in prompt.lower()


def test_the_panel_sees_the_preferences(prompts: list[str]) -> None:
    state = stated_state()
    opening_turn_node(OpeningInput(speaker="PM", user_idea="A climbing log", max_rounds=3, preferences=STATED))  # type: ignore[typeddict-item]
    discussion_node(state)  # type: ignore[arg-type]
    assert len(prompts) == 2
    for prompt in prompts:
        assert_hard_constraints(prompt)


def test_the_summarizer_and_its_questions_see_the_preferences(prompts: list[str]) -> None:
    summarizer_node(stated_state())  # type: ignore[arg-type]
    assert len(prompts) == 2
    for prompt in prompts:
        assert_hard_constraints(prompt)


def test_the_architect_treats_the_preferences_as_hard_constraints(prompts: list[str]) -> None:
    architect_node(stated_state())  # type: ignore[arg-type]
    (prompt,) = prompts
    assert_hard_constraints(prompt)


def test_the_strategy_sees_the_preferences(prompts: list[str]) -> None:
    state = stated_state()
    state["arch_choice"] = {"option": "A", "notes": ""}
    strategy_node(state)  # type: ignore[arg-type]
    (prompt,) = prompts
    assert_hard_constraints(prompt)


def test_every_blueprint_document_is_written_with_the_preferences(prompts: list[str]) -> None:
    state = stated_state()
    context = blueprint_context(state)  # type: ignore[arg-type]
    assert_hard_constraints(context)
    spec = bundle_file_plan(None)[0]
    generate_doc_node(
        DocInput(path=spec.relative_path, strategy=None, context_block=context, upstream={}, issues="", previous="")
    )
    (prompt,) = prompts
    assert_hard_constraints(prompt)


def test_without_preferences_no_prompt_mentions_them(prompts: list[str]) -> None:
    state = stated_state()
    state["preferences"] = ProjectPreferences().model_dump()
    architect_node(state)  # type: ignore[arg-type]
    summarizer_node(state)  # type: ignore[arg-type]
    assert blueprint_context(state) and not any("hard constraint" in p.lower() for p in [*prompts, blueprint_context(state)])  # type: ignore[arg-type]

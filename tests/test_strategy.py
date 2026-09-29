import pytest

from idea_to_mvp import llm
from idea_to_mvp.llm.structured import StructuredOutputError
from idea_to_mvp.nodes.strategy import fallback_strategy, strategy_node
from idea_to_mvp.schemas import ExecutionStrategy, Workstream


class FakeRuntime:
    def __init__(self, system_prompt: str = "system") -> None:
        self.llm = object()
        self.provider = "anthropic"
        self.model = "fake-model"
        self.max_tokens = 256
        self.system_prompt = system_prompt


def _state() -> dict:
    return {
        "user_idea": "idea",
        "summary": "summary",
        "generated_questions": ["1. Q?"],
        "user_answers": "answers",
        "architecture": "## Option A\n## Option B",
        "arch_choice": {"option": "A", "notes": ""},
    }


@pytest.fixture()
def fake_runtime(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(llm, "get_runtime", lambda key: FakeRuntime(f"system::{key}"))


def test_strategy_node_stores_the_validated_strategy_as_a_dict(fake_runtime, monkeypatch) -> None:
    chosen = ExecutionStrategy(
        mode="agent_team",
        reasoning="Independent workstreams.",
        workstreams=[
            Workstream(name="backend-api", focus="API", deliverables="REST API"),
            Workstream(name="web-ui", focus="UI", deliverables="Frontend"),
        ],
    )
    seen: dict = {}

    def fake_structured(runtime, messages, schema, *, fallback=None):
        seen["schema"] = schema
        seen["prompt"] = "\n".join(str(m.content) for m in messages)
        return chosen

    monkeypatch.setattr(llm, "invoke_structured", fake_structured)
    update = strategy_node(_state())
    assert seen["schema"] is ExecutionStrategy
    assert "User chose option: A" in seen["prompt"]
    strategy = update["execution_strategy"]
    assert strategy["mode"] == "agent_team"
    assert [w["name"] for w in strategy["workstreams"]] == ["backend-api", "web-ui"]
    assert update["stage"] == "strategy"


def test_strategy_node_passes_a_valid_fallback(fake_runtime, monkeypatch) -> None:
    captured = {}

    def fake_structured(runtime, messages, schema, *, fallback=None):
        captured["fallback"] = fallback
        return fallback()

    monkeypatch.setattr(llm, "invoke_structured", fake_structured)
    strategy = strategy_node(_state())["execution_strategy"]
    assert strategy["mode"] == "subagents"
    assert strategy["workstreams"], "fallback must still define at least one workstream"
    assert captured["fallback"] is fallback_strategy


def test_strategy_errors_are_not_swallowed_when_no_fallback_exists() -> None:
    assert issubclass(StructuredOutputError, RuntimeError)

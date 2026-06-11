import pytest
from langchain_core.messages import AIMessage

import agents


class FakeRuntime:
    def __init__(self, system_prompt: str = "system") -> None:
        self.llm = object()
        self.provider = "anthropic"
        self.model = "fake-model"
        self.max_tokens = 256
        self.system_prompt = system_prompt


STRATEGY_JSON = (
    '{"mode": "agent_team", "reasoning": "Independent workstreams.", '
    '"workstreams": [{"name": "backend-api", "focus": "API", "deliverables": "REST API"}, '
    '{"name": "web-ui", "focus": "UI", "deliverables": "Frontend"}]}'
)


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
    monkeypatch.setattr(agents, "get_runtime", lambda key: FakeRuntime(f"system::{key}"))


def test_strategy_node_parses_strict_json(fake_runtime, monkeypatch) -> None:
    monkeypatch.setattr(
        agents, "_invoke_with_runtime", lambda runtime, messages, **kw: AIMessage(content=STRATEGY_JSON)
    )
    update = agents.strategy_node(_state())
    strategy = update["execution_strategy"]
    assert strategy["mode"] == "agent_team"
    assert [w["name"] for w in strategy["workstreams"]] == ["backend-api", "web-ui"]
    assert update["stage"] == "strategy"


def test_strategy_node_strips_markdown_fences(fake_runtime, monkeypatch) -> None:
    fenced = f"```json\n{STRATEGY_JSON}\n```"
    monkeypatch.setattr(
        agents, "_invoke_with_runtime", lambda runtime, messages, **kw: AIMessage(content=fenced)
    )
    strategy = agents.strategy_node(_state())["execution_strategy"]
    assert strategy["mode"] == "agent_team"


def test_strategy_node_falls_back_on_invalid_json(fake_runtime, monkeypatch) -> None:
    monkeypatch.setattr(
        agents, "_invoke_with_runtime", lambda runtime, messages, **kw: AIMessage(content="not json at all")
    )
    strategy = agents.strategy_node(_state())["execution_strategy"]
    assert strategy["mode"] == "subagents"
    assert strategy["workstreams"], "fallback must still define at least one workstream"


def test_strategy_node_rejects_unknown_mode(fake_runtime, monkeypatch) -> None:
    bad_mode = STRATEGY_JSON.replace("agent_team", "swarm")
    monkeypatch.setattr(
        agents, "_invoke_with_runtime", lambda runtime, messages, **kw: AIMessage(content=bad_mode)
    )
    strategy = agents.strategy_node(_state())["execution_strategy"]
    assert strategy["mode"] == "subagents"

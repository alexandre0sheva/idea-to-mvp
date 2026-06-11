from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

import agents
import graph as graph_module


class FakeRuntime:
    def __init__(self, system_prompt: str = "system") -> None:
        self.llm = object()
        self.provider = "anthropic"
        self.model = "fake-model"
        self.max_tokens = 256
        self.system_prompt = system_prompt


def _base_state(max_rounds: int = 3) -> dict[str, Any]:
    return {
        "user_idea": "idea",
        "discussion_history": [HumanMessage(content="idea")],
        "summary": "",
        "generated_questions": [],
        "user_answers": "",
        "architecture": "",
        "plan_offer_question": "",
        "plan_decision": {"generate": False, "notes": ""},
        "project_bundle_dir": "",
        "project_bundle_files": [],
        "project_bundle_summary": "",
        "stage": "discussion",
        "next_speaker": "PM",
        "max_rounds": max_rounds,
        "turn_count": 0,
    }


@pytest.fixture()
def fake_pipeline(monkeypatch: pytest.MonkeyPatch, tmp_path):
    captured_prompts: list[str] = []

    def fake_get_runtime(role_key: str) -> FakeRuntime:
        return FakeRuntime(system_prompt=f"system::{role_key}")

    def fake_invoke(runtime, messages, *, max_tokens=None):
        captured_prompts.append("\n".join(str(m.content) for m in messages))
        return AIMessage(
            content=(
                "1. What is the MVP scope?\n"
                "2. Who is the first user?\n"
                "3. What data model is required?\n"
                "4. Which integrations are mandatory?\n"
                "5. What latency constraints exist?"
            )
        )

    monkeypatch.setattr(agents, "get_runtime", fake_get_runtime)
    monkeypatch.setattr(agents, "_invoke_with_runtime", fake_invoke)
    monkeypatch.setattr(agents, "_project_bundle_root", lambda: tmp_path / "project_blueprints")
    graph_module.clear_graph_cache()
    yield captured_prompts
    graph_module.clear_graph_cache()


def test_route_after_discussion_loops_until_max() -> None:
    state = _base_state(max_rounds=3)
    state["turn_count"] = 2
    assert agents.route_after_discussion(state) == "discussion"


def test_route_after_discussion_moves_to_summarizer() -> None:
    state = _base_state(max_rounds=3)
    state["turn_count"] = 3
    assert agents.route_after_discussion(state) == "summarizer"


def test_route_after_plan_gate() -> None:
    declined = _base_state()
    declined["plan_decision"] = {"generate": False, "notes": ""}
    assert agents.route_after_plan_gate(declined) == "__end__"
    accepted = _base_state()
    accepted["plan_decision"] = {"generate": True, "notes": "go"}
    assert agents.route_after_plan_gate(accepted) == "plan_bundle"


def test_full_pipeline_pauses_resumes_and_declines(fake_pipeline) -> None:
    g = graph_module.build_graph(True)
    config = {"configurable": {"thread_id": "lifecycle-decline"}}

    events = list(g.stream(_base_state(max_rounds=3), config=config, stream_mode="updates"))
    interrupts = [e["__interrupt__"][0] for e in events if "__interrupt__" in e]
    assert len(interrupts) == 1
    assert interrupts[0].value["kind"] == "answers"
    assert len(interrupts[0].value["questions"]) == 5

    events = list(g.stream(Command(resume="1. Solo founders."), config=config, stream_mode="updates"))
    interrupts = [e["__interrupt__"][0] for e in events if "__interrupt__" in e]
    assert len(interrupts) == 1
    assert interrupts[0].value["kind"] == "plan_gate"
    assert interrupts[0].value["question"].strip()

    events = list(g.stream(Command(resume={"generate": False, "notes": ""}), config=config, stream_mode="updates"))
    assert not any("__interrupt__" in e for e in events)
    snapshot = g.get_state(config)
    assert snapshot.next == ()
    assert snapshot.values["plan_decision"] == {"generate": False, "notes": ""}
    assert snapshot.values["user_answers"] == "1. Solo founders."
    assert snapshot.values["stage"] == "done"


def test_full_pipeline_accept_generates_bundle(fake_pipeline, tmp_path) -> None:
    g = graph_module.build_graph(True)
    config = {"configurable": {"thread_id": "lifecycle-accept"}}

    list(g.stream(_base_state(max_rounds=3), config=config, stream_mode="updates"))
    list(g.stream(Command(resume="Answers."), config=config, stream_mode="updates"))
    list(g.stream(Command(resume={"generate": True, "notes": "keep it lean"}), config=config, stream_mode="updates"))

    snapshot = g.get_state(config)
    assert snapshot.next == ()
    assert snapshot.values["plan_decision"] == {"generate": True, "notes": "keep it lean"}
    assert snapshot.values["project_bundle_files"] == [
        "AGENTS.md",
        "contracts/AGENTS.md",
        "application/AGENTS.md",
        "quality/AGENTS.md",
        "plan.md",
    ]
    bundle_dir = tmp_path / "project_blueprints"
    assert any(bundle_dir.iterdir())


def test_discussion_prompt_is_round_aware(fake_pipeline) -> None:
    state = _base_state(max_rounds=6)  # 2 rounds per speaker
    agents.discussion_node(state)
    assert "round 1 of 2" in fake_pipeline[-1]
    state["turn_count"] = 5  # last turn of final round
    agents.discussion_node(state)
    assert "round 2 of 2" in fake_pipeline[-1]
    assert "FINAL round" in fake_pipeline[-1]

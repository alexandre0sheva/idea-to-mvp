from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from idea_to_mvp import graph as graph_module
from idea_to_mvp import implementer, llm
from idea_to_mvp.config import clear_settings_cache
from idea_to_mvp.demo.fixtures import respond_structured
from idea_to_mvp.implementation import verify as verify_lanes
from idea_to_mvp.implementation.verify import FixOutcome, LaneOutcome, LaneReport
from idea_to_mvp.nodes.discussion import discussion_node
from idea_to_mvp.nodes.gates import route_after_plan_gate
from idea_to_mvp.state import make_initial_state


class FakeRuntime:
    def __init__(self, system_prompt: str = "system") -> None:
        self.llm = object()
        self.provider = "anthropic"
        self.model = "fake-model"
        self.max_tokens = 256
        self.system_prompt = system_prompt


def _base_state(max_rounds: int = 3) -> dict[str, Any]:
    state = dict(make_initial_state("idea", 1))
    state["max_rounds"] = max_rounds
    return state


@pytest.fixture()
def fake_pipeline(monkeypatch: pytest.MonkeyPatch, tmp_path):
    captured_prompts: list[str] = []

    def fake_get_runtime(role_key: str) -> FakeRuntime:
        return FakeRuntime(system_prompt=f"system::{role_key}")

    def fake_invoke(runtime, messages, *, max_tokens=None):
        captured_prompts.append("\n".join(str(m.content) for m in messages))
        return (
            "1. What is the MVP scope?\n"
            "2. Who is the first user?\n"
            "3. What data model is required?\n"
            "4. Which integrations are mandatory?\n"
            "5. What latency constraints exist?"
        )

    monkeypatch.setattr(llm, "get_runtime", fake_get_runtime)
    monkeypatch.setattr(llm, "invoke_text", fake_invoke)
    monkeypatch.setattr(
        llm,
        "invoke_structured",
        lambda runtime, messages, schema, *, fallback=None: respond_structured(schema, messages),
    )
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    clear_settings_cache()
    yield captured_prompts
    clear_settings_cache()


def _interrupts(events: list[dict]) -> list:
    return [e["__interrupt__"][0] for e in events if "__interrupt__" in e]


def test_route_after_plan_gate() -> None:
    declined = _base_state()
    declined["plan_decision"] = {"generate": False, "notes": ""}
    assert route_after_plan_gate(declined) == "__end__"
    accepted = _base_state()
    accepted["plan_decision"] = {"generate": True, "notes": "go"}
    assert route_after_plan_gate(accepted) == "plan_bundle"


def test_full_pipeline_pauses_resumes_and_declines(fake_pipeline) -> None:
    g = graph_module.build_graph(MemorySaver())
    config = {"configurable": {"thread_id": "lifecycle-decline"}}

    events = list(g.stream(_base_state(max_rounds=3), config=config, stream_mode="updates"))
    interrupts = _interrupts(events)
    assert len(interrupts) == 1
    assert interrupts[0].value["kind"] == "answers"
    assert len(interrupts[0].value["questions"]) == 5

    events = list(g.stream(Command(resume="1. Solo founders."), config=config, stream_mode="updates"))
    interrupts = _interrupts(events)
    assert len(interrupts) == 1
    assert interrupts[0].value["kind"] == "arch_choice"
    assert interrupts[0].value["architecture"].strip()

    events = list(
        g.stream(Command(resume={"option": "A", "notes": "keep it simple"}), config=config, stream_mode="updates")
    )
    interrupts = _interrupts(events)
    assert len(interrupts) == 1
    assert interrupts[0].value["kind"] == "plan_gate"
    assert interrupts[0].value["question"].strip()

    events = list(g.stream(Command(resume={"generate": False, "notes": ""}), config=config, stream_mode="updates"))
    assert not _interrupts(events)
    snapshot = g.get_state(config)
    assert snapshot.next == ()
    assert snapshot.values["plan_decision"] == {"generate": False, "notes": ""}
    assert snapshot.values["user_answers"] == "1. Solo founders."
    assert snapshot.values["arch_choice"] == {"option": "A", "notes": "keep it simple"}
    assert snapshot.values["execution_strategy"]["mode"] in ("subagents", "agent_team")
    assert snapshot.values["stage"] == "done"


def test_full_pipeline_accept_generates_bundle(fake_pipeline, tmp_path) -> None:
    g = graph_module.build_graph(MemorySaver())
    config = {"configurable": {"thread_id": "lifecycle-accept"}}

    list(g.stream(_base_state(max_rounds=3), config=config, stream_mode="updates"))
    list(g.stream(Command(resume="Answers."), config=config, stream_mode="updates"))
    list(g.stream(Command(resume={"option": "B", "notes": ""}), config=config, stream_mode="updates"))
    events = list(
        g.stream(Command(resume={"generate": True, "notes": "keep it lean"}), config=config, stream_mode="updates")
    )

    snapshot = g.get_state(config)
    assert snapshot.values["plan_decision"] == {"generate": True, "notes": "keep it lean"}
    assert snapshot.values["project_bundle_files"]
    bundle_dir = tmp_path / "blueprints"
    assert any(bundle_dir.iterdir())
    interrupts = _interrupts(events)
    assert len(interrupts) == 1
    assert interrupts[0].value["kind"] == "implement_gate"
    assert interrupts[0].value["question"].strip()


async def test_implement_gate_accept_builds_verifies_and_reports(fake_pipeline, monkeypatch, tmp_path) -> None:
    workspace = tmp_path / "projects" / "ws"
    workspace.mkdir(parents=True)
    (workspace / "README.md").write_text("readme")

    fixes: list[list[str]] = []
    rounds: list[int] = []

    async def run_lane(ws, lane, settings, *, budget_usd, emit, **_):
        if lane == "tests":
            rounds.append(1)
        failing = lane == "tests" and len(rounds) == 1  # the first round fails, the fix works
        report = LaneReport(
            lane=lane, passed=not failing, commands_run=[], failures=["2 failed"] if failing else [], summary=lane
        )
        return LaneOutcome(report)

    async def run_fix(ws, failures, settings, *, budget_usd, emit, **_):
        fixes.append(failures)
        return FixOutcome(True, "fixed")

    monkeypatch.setattr(implementer, "prepare_workspace", lambda bundle, root: workspace)
    monkeypatch.setattr(implementer, "run_implementation", lambda ws, strategy, settings: "Implemented everything.")
    monkeypatch.setattr(verify_lanes, "run_lane", run_lane)
    monkeypatch.setattr(verify_lanes, "run_fix", run_fix)

    g = graph_module.build_graph(MemorySaver())
    config = {"configurable": {"thread_id": "lifecycle-implement"}}
    # The implementer node is async (it can be cancelled and publishes live events), so drive the graph with astream.
    async def drive(payload) -> list[dict]:
        return [event async for event in g.astream(payload, config=config, stream_mode="updates")]

    await drive(_base_state(max_rounds=3))
    await drive(Command(resume="Answers."))
    await drive(Command(resume={"option": "A", "notes": ""}))
    await drive(Command(resume={"generate": True, "notes": ""}))
    events = await drive(Command(resume={"implement": True, "notes": "go"}))

    # The delivered version waits at the iterate gate: the user may request changes for the next one.
    assert [i.value["kind"] for i in _interrupts(events)] == ["iterate_gate"]
    snapshot = await g.aget_state(config)
    assert snapshot.next == ("iterate_gate",)
    assert snapshot.values["implement_decision"] == {"implement": True, "notes": "go", "parallel": 0}
    assert snapshot.values["workspace_dir"] == str(workspace)
    assert snapshot.values["implementation_log"] == "Implemented everything."
    assert snapshot.values["verification"]["passed"] is True
    assert snapshot.values["verification"]["attempts"] == 1
    assert fixes == [["[tests] 2 failed"]]
    assert "Delivery report" in snapshot.values["delivery_report"]
    assert str(workspace) in snapshot.values["delivery_report"]
    assert snapshot.values["stage"] == "done"

    await drive(Command(resume={"iterate": False, "feedback": ""}))
    assert (await g.aget_state(config)).next == ()


def test_implement_gate_decline_ends_run(fake_pipeline) -> None:
    g = graph_module.build_graph(MemorySaver())
    config = {"configurable": {"thread_id": "lifecycle-no-implement"}}
    list(g.stream(_base_state(max_rounds=3), config=config, stream_mode="updates"))
    list(g.stream(Command(resume="Answers."), config=config, stream_mode="updates"))
    list(g.stream(Command(resume={"option": "A", "notes": ""}), config=config, stream_mode="updates"))
    list(g.stream(Command(resume={"generate": True, "notes": ""}), config=config, stream_mode="updates"))
    events = list(g.stream(Command(resume={"implement": False, "notes": ""}), config=config, stream_mode="updates"))

    assert not _interrupts(events)
    snapshot = g.get_state(config)
    assert snapshot.next == ()
    assert snapshot.values["implement_decision"] == {"implement": False, "notes": "", "parallel": 0}
    assert snapshot.values["workspace_dir"] == ""
    assert snapshot.values["stage"] == "done"


def test_discussion_prompt_is_round_aware(fake_pipeline) -> None:
    state = _base_state(max_rounds=6)  # 2 rounds per speaker
    discussion_node(state)
    assert "round 1 of 2" in fake_pipeline[-1]
    state["turn_count"] = 5  # last turn of final round
    discussion_node(state)
    assert "round 2 of 2" in fake_pipeline[-1]
    assert "FINAL round" in fake_pipeline[-1]

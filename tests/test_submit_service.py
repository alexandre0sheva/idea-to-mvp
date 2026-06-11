from langchain_core.messages import AIMessage
from langgraph.types import Command

from config import Settings
from submit_service import (
    ARCH_CHOICE_A,
    ARCH_CHOICE_B,
    IMPL_CHOICE_SKIP,
    IMPL_CHOICE_START,
    MODE_ANSWERS,
    MODE_ARCH_CHOICE,
    MODE_DONE,
    MODE_IDEA,
    MODE_IMPL_GATE,
    MODE_PLAN_GATE,
    PLAN_CHOICE_GENERATE,
    PLAN_CHOICE_SKIP,
    AppContext,
    SubmitService,
)

# Output tuple positions (see SubmitService._pack):
# 0 status, 1 chatbot, 2 input_tb, 3 rounds_sl, 4 decision_radio, 5 run_btn,
# 6 thread_id, 7 mode, 8 turns_state, 9 transcript_state, 10 chat_state
POS_THREAD = 6
POS_MODE = 7
POS_TRANSCRIPT = 9
POS_CHAT = 10


class FakeInterrupt:
    def __init__(self, value):
        self.value = value


class DummyGraph:
    def __init__(self, events):
        self._events = events
        self.calls = []

    def stream(self, payload, *args, **kwargs):
        self.calls.append(payload)
        yield from self._events


def _service(events):
    graph = DummyGraph(events)
    return SubmitService(AppContext(settings=Settings(), graph=graph)), graph


def test_clear_session_resets_core_state() -> None:
    service, _ = _service([])
    result = service.clear_session()
    assert result[POS_MODE] == MODE_IDEA
    assert result[POS_TRANSCRIPT] == []
    assert result[POS_CHAT] == []


def test_idea_mode_runs_to_answers_gate() -> None:
    events = [
        {
            "discussion": {
                "discussion_history": [AIMessage(content="Panel output", name="pm")],
                "turn_count": 1,
                "next_speaker": "Tech Lead",
            }
        },
        {"summarizer": {"summary": "Executive summary", "generated_questions": ["1. Who is the user?"]}},
        {
            "__interrupt__": (
                FakeInterrupt({"kind": "answers", "questions": ["1. Who is the user?"], "summary": "Executive summary"}),
            )
        },
    ]
    service, graph = _service(events)
    outputs = list(service.handle_submit("Build x", 1, "", "thread-1", MODE_IDEA, [], [], []))
    final = outputs[-1]
    assert final[POS_MODE] == MODE_ANSWERS
    assert any("Who is the user?" in m["content"] for m in final[POS_CHAT])
    assert isinstance(graph.calls[0], dict)
    assert graph.calls[0]["user_idea"] == "Build x"


def test_answers_mode_resumes_to_arch_choice() -> None:
    events = [
        {"architect": {"architecture": "## Option A\nSimple stack"}},
        {
            "__interrupt__": (
                FakeInterrupt({"kind": "arch_choice", "question": "Pick A or B?", "architecture": "## Option A"}),
            )
        },
    ]
    service, graph = _service(events)
    outputs = list(service.handle_submit("1. Solo founders.", 1, "", "thread-2", MODE_ANSWERS, [], [], []))
    final = outputs[-1]
    assert final[POS_MODE] == MODE_ARCH_CHOICE
    assert isinstance(graph.calls[0], Command)
    assert graph.calls[0].resume == "1. Solo founders."
    assert any("Architect" in m["content"] for m in final[POS_CHAT])
    assert "Pick A or B?" in final[POS_CHAT][-1]["content"]


def test_arch_choice_resumes_to_plan_gate() -> None:
    events = [
        {
            "strategy": {
                "execution_strategy": {
                    "mode": "subagents",
                    "reasoning": "Small MVP.",
                    "workstreams": [{"name": "core", "focus": "all", "deliverables": "app"}],
                }
            }
        },
        {"planner_offer": {"plan_offer_question": "Generate the pack?"}},
        {
            "__interrupt__": (
                FakeInterrupt({"kind": "plan_gate", "question": "Generate the pack?", "architecture": "## A"}),
            )
        },
    ]
    service, graph = _service(events)
    outputs = list(
        service.handle_submit("prefer simple", 1, ARCH_CHOICE_B, "thread-3", MODE_ARCH_CHOICE, [], [], [])
    )
    final = outputs[-1]
    assert final[POS_MODE] == MODE_PLAN_GATE
    assert graph.calls[0].resume == {"option": "B", "notes": "prefer simple"}
    assert any("subagents" in m["content"] for m in final[POS_CHAT])
    assert "Generate the pack?" in final[POS_CHAT][-1]["content"]


def test_arch_choice_option_a_parsed() -> None:
    events = [
        {
            "__interrupt__": (
                FakeInterrupt({"kind": "plan_gate", "question": "Generate?", "architecture": ""}),
            )
        },
    ]
    service, graph = _service(events)
    list(service.handle_submit("", 1, ARCH_CHOICE_A, "thread-3b", MODE_ARCH_CHOICE, [], [], []))
    assert graph.calls[0].resume == {"option": "A", "notes": ""}


def test_plan_gate_generate_resumes_to_implement_gate() -> None:
    events = [
        {"plan_bundle": {"project_bundle_summary": "Generated pack in `dir`.", "project_bundle_dir": "dir"}},
        {
            "__interrupt__": (
                FakeInterrupt({"kind": "implement_gate", "question": "Build it now?", "bundle_dir": "dir"}),
            )
        },
    ]
    service, graph = _service(events)
    outputs = list(
        service.handle_submit("keep it lean", 1, PLAN_CHOICE_GENERATE, "thread-4", MODE_PLAN_GATE, [], [], [])
    )
    final = outputs[-1]
    assert final[POS_MODE] == MODE_IMPL_GATE
    assert graph.calls[0].resume == {"generate": True, "notes": "keep it lean"}
    assert any("Generated pack" in m["content"] for m in final[POS_CHAT])
    assert "Build it now?" in final[POS_CHAT][-1]["content"]


def test_plan_gate_skip_finishes_session() -> None:
    service, graph = _service([])
    outputs = list(service.handle_submit("", 1, PLAN_CHOICE_SKIP, "thread-5", MODE_PLAN_GATE, [], [], []))
    final = outputs[-1]
    assert final[POS_MODE] == MODE_DONE
    assert graph.calls[0].resume == {"generate": False, "notes": ""}


def test_implement_gate_start_runs_to_delivery_report() -> None:
    events = [
        {"implementer": {"workspace_dir": "/tmp/ws", "implementation_log": "Built everything."}},
        {"verifier": {"verification": {"passed": True, "attempts": 0, "report": "VERDICT: PASS"}}},
        {"delivery_report": {"delivery_report": "## Delivery report\n\nAll done."}},
    ]
    service, graph = _service(events)
    outputs = list(
        service.handle_submit("notes for builder", 1, IMPL_CHOICE_START, "thread-6", MODE_IMPL_GATE, [], [], [])
    )
    final = outputs[-1]
    assert final[POS_MODE] == MODE_DONE
    assert graph.calls[0].resume == {"implement": True, "notes": "notes for builder"}
    assert any("Built everything." in m["content"] for m in final[POS_CHAT])
    assert any("Delivery report" in m["content"] for m in final[POS_CHAT])


def test_implement_gate_skip_finishes_session() -> None:
    service, graph = _service([])
    outputs = list(service.handle_submit("", 1, IMPL_CHOICE_SKIP, "thread-7", MODE_IMPL_GATE, [], [], []))
    final = outputs[-1]
    assert final[POS_MODE] == MODE_DONE
    assert graph.calls[0].resume == {"implement": False, "notes": ""}


def test_error_mid_run_reports_stage_and_keeps_mode() -> None:
    class ExplodingGraph:
        calls: list = []

        def stream(self, payload, *args, **kwargs):
            raise TimeoutError("model timed out")
            yield  # pragma: no cover

    service = SubmitService(AppContext(settings=Settings(), graph=ExplodingGraph()))
    outputs = list(service.handle_submit("Answers", 1, "", "thread-8", MODE_ANSWERS, [], [], []))
    final = outputs[-1]
    assert final[POS_MODE] == MODE_ANSWERS  # retryable
    assert any("error" in m["content"].lower() for m in final[POS_CHAT])

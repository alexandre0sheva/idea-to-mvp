from langchain_core.messages import AIMessage

from config import Settings
from submit_service import AppContext, SubmitService


class DummyGraph:
    def __init__(self, events: list[dict]):
        self._events = events

    def stream(self, *_args, **_kwargs):
        yield from self._events


def test_clear_session_resets_core_state() -> None:
    service = SubmitService(AppContext(settings=Settings(), graph=DummyGraph([])))
    result = service.clear_session()
    assert result[6] == "idea"
    assert result[7] == []
    assert result[8] == ""


def test_idea_mode_progresses_to_answers() -> None:
    events = [
        {
            "discussion": {
                "discussion_history": [AIMessage(content="Panel output", name="pm")],
                "turn_count": 1,
                "next_speaker": "Tech Lead",
            }
        },
        {"summarizer": {"summary": "Executive summary", "generated_questions": ["1. Who is the user?"]}},
    ]
    service = SubmitService(AppContext(settings=Settings(), graph=DummyGraph(events)))
    outputs = list(service.handle_submit("Build x", 1, "thread-1", "idea", [], "", [], []))
    final = outputs[-1]
    assert final[6] == "answers"
    assert final[7] == ["1. Who is the user?"]
    assert final[8] == "Executive summary"


def test_answers_mode_generates_architecture() -> None:
    events = [{"architect": {"architecture": "## Option A\nSimple stack"}}]
    service = SubmitService(AppContext(settings=Settings(), graph=DummyGraph(events)))
    chat_state = [{"role": "user", "content": "Original idea"}]
    outputs = list(
        service.handle_submit(
            "User clarification",
            1,
            "thread-2",
            "answers",
            ["1. What scope?"],
            "Summary text",
            [],
            chat_state,
        )
    )
    final = outputs[-1]
    assert final[6] == "answers"
    assert "Architect" in final[1][-1]["content"]

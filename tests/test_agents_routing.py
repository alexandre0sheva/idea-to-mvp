from agents import route_after_discussion, route_from_start


def _base_state() -> dict:
    return {
        "user_idea": "idea",
        "discussion_history": [],
        "summary": "",
        "generated_questions": [],
        "user_answers": "",
        "architecture": "",
        "phase": "idea",
        "next_speaker": "PM",
        "max_rounds": 3,
        "turn_count": 0,
    }


def test_route_after_discussion_loops_until_max() -> None:
    state = _base_state()
    state["turn_count"] = 2
    state["max_rounds"] = 3
    assert route_after_discussion(state) == "discussion"


def test_route_after_discussion_moves_to_summarizer() -> None:
    state = _base_state()
    state["turn_count"] = 3
    state["max_rounds"] = 3
    assert route_after_discussion(state) == "summarizer"


def test_route_from_start_architect_in_answers_phase() -> None:
    state = _base_state()
    state["phase"] = "answers"
    assert route_from_start(state) == "architect"

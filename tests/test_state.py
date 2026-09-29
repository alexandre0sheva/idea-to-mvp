from typing import get_type_hints

from idea_to_mvp.state import IdeaDiscussionState, make_initial_state


def test_initial_state_has_all_state_keys() -> None:
    state = make_initial_state("An idea", 2)
    assert set(state) == set(get_type_hints(IdeaDiscussionState))


def test_initial_state_seeds_panel_from_idea_and_rounds() -> None:
    state = make_initial_state("An idea", 2)
    assert state["user_idea"] == "An idea"
    assert state["discussion_history"][0].content == "An idea"
    assert state["max_rounds"] == 6  # 2 rounds x 3 speakers
    assert state["next_speaker"] == "PM"
    assert state["turn_count"] == 0

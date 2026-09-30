from idea_to_mvp.ui.render import moderator_banner, openings_row, questions_block, turn_block


def test_questions_block_shows_typed_details_when_available() -> None:
    questions = [f"{i}. Question {i}?" for i in range(1, 6)]
    items = [
        {"question": f"Question {i}?", "why_it_matters": f"Reason {i}", "suggested_answer": f"Suggestion {i}"}
        for i in range(1, 6)
    ]
    html = questions_block(questions, items)
    assert "Reason 3" in html and "Suggestion 5" in html
    assert "Reason" not in questions_block(questions)  # plain strings still render


def test_questions_block_ignores_mismatched_item_counts() -> None:
    assert "Reason" not in questions_block(["1. Only one?"], [{"why_it_matters": "Reason"}, {}])


def test_opening_statements_are_one_row_of_three_cards() -> None:
    html = openings_row([("PM", "Scope small."), ("Tech Lead", "Monolith."), ("Skeptic", "Why build it?")])
    assert html.count("class='opening-row'") == 1 and html.count("speaker-card") == 3
    assert html.index("Scope small.") < html.index("Monolith.") < html.index("Why build it?")
    assert "speaker-pm" in html and "speaker-tech-lead" in html and "speaker-skeptic" in html


def test_the_opening_row_can_be_collapsed_and_marks_live_cards() -> None:
    turns = [("PM", "a"), ("Skeptic", "b")]
    assert " open" in openings_row(turns) and " open" not in openings_row(turns, collapsed=True)
    assert "live-turn" in openings_row(turns, live=True) and "live-turn" not in openings_row(turns)


def test_a_collapsed_turn_is_closed_and_a_recent_one_is_open() -> None:
    assert "<details class='speaker-card speaker-pm' open>" in turn_block("PM", "x")
    assert " open" not in turn_block("PM", "x", collapsed=True)


def test_panel_text_is_escaped_in_rows_and_turns() -> None:
    evil = "<script>alert(1)</script>"
    for html in (openings_row([("PM", evil)]), turn_block("PM", evil), moderator_banner(evil)):
        assert "<script>" not in html and "&lt;script&gt;" in html


def test_the_moderators_note_is_a_slim_banner() -> None:
    html = moderator_banner("All three agree on the scope.")
    assert "moderator-banner" in html and "All three agree on the scope." in html
    assert "speaker-card" not in html and "<details" not in html


def test_each_card_of_the_opening_row_knows_whether_it_is_still_being_written() -> None:
    html = openings_row([("PM", "done"), ("Skeptic", "writing")], live=[False, True])
    assert html.count("live-turn") == 1 and html.index("live-turn") > html.index("done")

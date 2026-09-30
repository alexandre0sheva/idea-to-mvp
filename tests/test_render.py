from idea_to_mvp.ui.render import questions_block


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

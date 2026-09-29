from idea_to_mvp.ui.render import PIPELINE_STAGES, questions_block, stage_tracker


def test_stage_tracker_marks_active_and_done() -> None:
    html = stage_tracker("strategy")
    assert html.count("stage-pill") == len(PIPELINE_STAGES)
    assert ">Strategy</span>" in html
    assert "stage-pill active'>Strategy" in html
    assert "stage-pill done'>Panel" in html
    assert "stage-pill todo'>Blueprint" in html


def test_stage_tracker_aliases_map_to_pipeline_stages() -> None:
    assert "stage-pill active'>Architecture" in stage_tracker("architecture")
    assert "stage-pill active'>Blueprint" in stage_tracker("plan_gate")
    assert "stage-pill active'>Implementation" in stage_tracker("implement_gate")
    assert "stage-pill active'>Verification" in stage_tracker("report")


def test_stage_tracker_done_marks_everything_complete() -> None:
    html = stage_tracker("done")
    assert "stage-pill todo" not in html
    assert "stage-pill active'>Done" in html


def test_stage_tracker_unknown_stage_defaults_to_start() -> None:
    assert "stage-pill active'>Panel" in stage_tracker("nonsense")


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

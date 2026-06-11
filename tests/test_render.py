from render import PIPELINE_STAGES, stage_tracker


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

import json
from pathlib import Path

from idea_to_mvp.implementation.progress import (
    TaskResult,
    load_progress,
    progress_path,
    save_result,
)


def result(task_id: str = "T01", status: str = "done", **overrides) -> TaskResult:
    fields = {"task_id": task_id, "status": status, "summary": "Built it.", "cost_usd": 0.5, "turns": 9, "session_id": "s-1", "commit": "abc1234"}
    fields.update(overrides)
    return fields  # type: ignore[return-value]


def test_progress_lives_in_a_hidden_folder_of_the_workspace(tmp_path: Path) -> None:
    assert progress_path(tmp_path) == tmp_path / ".idea-to-mvp" / "progress.json"


def test_a_workspace_without_progress_has_none(tmp_path: Path) -> None:
    assert load_progress(tmp_path) == {}


def test_results_round_trip_and_accumulate(tmp_path: Path) -> None:
    save_result(tmp_path, result("T01"))
    save_result(tmp_path, result("T02", "failed", summary="broke", commit=None, session_id=None))
    loaded = load_progress(tmp_path)
    assert list(loaded) == ["T01", "T02"] and loaded["T02"]["status"] == "failed" and loaded["T02"]["commit"] is None
    assert loaded["T01"] == result("T01")


def test_saving_again_replaces_the_task_entry(tmp_path: Path) -> None:
    save_result(tmp_path, result("T01", "failed", summary="first try"))
    save_result(tmp_path, result("T01", "done", summary="second try"))
    assert load_progress(tmp_path)["T01"]["summary"] == "second try" and len(load_progress(tmp_path)) == 1


def test_a_corrupt_or_malformed_file_is_treated_as_no_progress(tmp_path: Path) -> None:
    path = progress_path(tmp_path)
    path.parent.mkdir(parents=True)
    for bad in ("{not json", "[]", json.dumps({"T01": "nope"}), json.dumps({"T01": {"status": "done"}})):
        path.write_text(bad)
        assert load_progress(tmp_path) == {}, bad


def test_saving_over_a_corrupt_file_starts_fresh(tmp_path: Path) -> None:
    path = progress_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("garbage")
    save_result(tmp_path, result("T01"))
    assert list(load_progress(tmp_path)) == ["T01"]


def test_the_file_is_replaced_atomically_with_no_temp_files_left(tmp_path: Path) -> None:
    save_result(tmp_path, result("T01"))
    assert [p.name for p in progress_path(tmp_path).parent.iterdir()] == ["progress.json"]

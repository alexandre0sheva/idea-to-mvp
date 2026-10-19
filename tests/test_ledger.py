"""The run ledger: one row per agent session, and the check that its total equals the agent spend in `usage`."""

from pathlib import Path

from idea_to_mvp.implementation.ledger import (
    LedgerRow,
    agent_spend,
    append_row,
    check_ledger,
    ledger_path,
    ledger_total,
    load_ledger,
    task_trajectories,
)
from idea_to_mvp.usage import UsageRecord


def row(label: str = "T01", cost: float = 0.5, **overrides: object) -> LedgerRow:
    fields: dict = {
        "kind": "task",
        "label": label,
        "task_id": label,
        "iteration": 1,
        "attempt": 1,
        "model": "m",
        "cost_usd": cost,
        "turns": 4,
        "outcome": "success",
        "session_id": f"s-{label}",
        "trajectory": f".idea-to-mvp/trajectories/{label}.md",
        "started_at": 100.0,
        "ended_at": 160.0,
    }
    fields.update(overrides)
    return fields  # type: ignore[return-value]


def usage(role: str, cost: float | None) -> UsageRecord:
    return {"role": role, "provider": "p", "model": "m", "input_tokens": 1, "output_tokens": 1, "cost_usd": cost}


def test_rows_are_appended_in_order_to_a_ledger_inside_the_orchestrator_folder(tmp_path: Path) -> None:
    append_row(tmp_path, row("T01"))
    append_row(tmp_path, row("T02", kind="resolver", task_id="T02"))
    assert ledger_path(tmp_path) == tmp_path / ".idea-to-mvp" / "ledger.json"
    assert [(r["label"], r["kind"]) for r in load_ledger(tmp_path)] == [("T01", "task"), ("T02", "resolver")]
    assert [p.name for p in ledger_path(tmp_path).parent.iterdir()] == ["ledger.json"]  # no temp file left behind


def test_a_missing_corrupt_or_malformed_ledger_reads_as_empty(tmp_path: Path) -> None:
    assert load_ledger(tmp_path) == []
    path = ledger_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert load_ledger(tmp_path) == []
    path.write_text('[{"label": "T01"}]')  # a row without the fields the rest of the code relies on
    assert load_ledger(tmp_path) == []


def test_appending_to_a_corrupt_ledger_starts_a_new_one_instead_of_failing(tmp_path: Path) -> None:
    path = ledger_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("garbage")
    append_row(tmp_path, row("T01"))
    assert [r["label"] for r in load_ledger(tmp_path)] == ["T01"]


def test_the_total_adds_every_session_whatever_its_outcome() -> None:
    rows = [row("T01", 0.5), row("T02", 0.25, outcome="failed"), row("lane-tests", 0.1, kind="lane", task_id=None)]
    assert ledger_total(rows) == 0.85


def test_agent_spend_counts_only_the_agent_roles_of_usage() -> None:
    records = [usage("implementer", 0.5), usage("verifier", 0.1), usage("fixer", 0.2), usage("pm", None), usage("architect", 0.9)]
    assert round(agent_spend(records), 6) == 0.8  # panel and planning calls are not agent sessions


def test_the_check_passes_when_the_ledger_equals_the_agent_spend(tmp_path: Path) -> None:
    append_row(tmp_path, row("T01", 0.5))
    append_row(tmp_path, row("lane-tests", 0.25, kind="lane", task_id=None))
    check = check_ledger(tmp_path, [usage("implementer", 0.5), usage("verifier", 0.25), usage("pm", None)])
    assert check.ok and check.ledger_usd == 0.75 and check.usage_usd == 0.75 and check.message == ""


def test_the_check_names_both_totals_when_they_differ(tmp_path: Path) -> None:
    append_row(tmp_path, row("T01", 0.5))
    check = check_ledger(tmp_path, [usage("implementer", 0.3)])
    assert not check.ok
    assert "$0.50" in check.message and "$0.30" in check.message


def test_an_empty_ledger_and_no_agent_spend_agree(tmp_path: Path) -> None:
    assert check_ledger(tmp_path, [usage("pm", None)]).ok


def test_each_task_links_to_the_trajectory_of_its_latest_session(tmp_path: Path) -> None:
    append_row(tmp_path, row("T01"))
    append_row(tmp_path, row("T01-2", attempt=2, task_id="T01"))
    append_row(tmp_path, row("T02-resolver", kind="resolver", task_id="T02"))
    append_row(tmp_path, row("lane-tests", kind="lane", task_id=None))
    links = task_trajectories(tmp_path)
    assert links == {"T01": ".idea-to-mvp/trajectories/T01-2.md"}  # resolvers and lanes are not the task's own session


def test_a_delivery_whose_ledger_and_usage_disagree_is_logged(tmp_path: Path, monkeypatch, caplog) -> None:
    import logging

    from idea_to_mvp.config import clear_settings_cache
    from idea_to_mvp.nodes.report import delivery_report_node

    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "out"))
    clear_settings_cache()
    try:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        append_row(workspace, row("T01", 1.0))
        state = {"workspace_dir": str(workspace), "verification": {"passed": False, "attempts": 0, "report": "", "lanes": []}, "usage": []}
        with caplog.at_level(logging.WARNING):
            delivery_report_node(state)  # type: ignore[arg-type]
        assert "run ledger adds up to $1.00" in caplog.text
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            delivery_report_node({**state, "usage": [usage("implementer", 1.0)]})  # type: ignore[arg-type]
        assert "run ledger" not in caplog.text
    finally:
        clear_settings_cache()

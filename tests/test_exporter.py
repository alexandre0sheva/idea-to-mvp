import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage

from idea_to_mvp.exporter import build_session_json, build_session_markdown, save_session_export
from idea_to_mvp.state import make_initial_state
from idea_to_mvp.ui.view import ViewEntry, transcript_from_state

SAVED_AT = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def lane(name: str, passed: bool = True, failures: list[str] | None = None) -> dict[str, Any]:
    return {
        "lane": name,
        "passed": passed,
        "commands_run": [{"command": f"check {name}", "exit_code": 0 if passed else 1}],
        "failures": failures or [],
        "summary": f"{name} summary",
    }


def finished_values(**overrides: Any) -> dict[str, Any]:
    """The state of a run that went all the way to a delivery."""
    values: dict[str, Any] = {
        **make_initial_state("Climbing log app", 2),
        "discussion_history": [AIMessage(content="Start small.", name="pm")],
        "convergence": {"converged": True, "reason": "Everyone agrees on a logging-first MVP."},
        "summary": "## Summary\nA climbing log.",
        "generated_questions": ["1. Who is it for?"],
        "user_answers": "1. Boulderers.",
        "architecture": "## Option A\nMonolith.",
        "arch_choice": {"option": "A", "notes": ""},
        "execution_strategy": {"mode": "workflow", "reasoning": "Small scope.", "workstreams": []},
        "plan_decision": {"generate": True, "notes": ""},
        "project_bundle_dir": "/tmp/bundle",
        "project_bundle_files": ["PRD.md", "ARCHITECTURE.md", "plan.md"],
        "project_bundle_summary": "Pack ready",
        "implement_decision": {"implement": True, "notes": ""},
        "implementation_log": "Built T01 and T02.",
        "verification": {
            "passed": False,
            "attempts": 1,
            "report": "merged report",
            "lanes": [lane("tests"), lane("quality", False, ["ruff: 2 errors"]), lane("requirements")],
        },
        "delivery_report": "## Delivery report\nv0.1 is ready.",
        "stage": "done",
    }
    return {**values, **overrides}


def export(values: dict[str, Any]) -> str:
    return build_session_markdown(
        entries=transcript_from_state(values, None), thread_id="t1", mode="done", saved_at=SAVED_AT
    )


def test_the_export_covers_the_run_after_the_architect() -> None:
    text = export(finished_values())
    for expected in ("Execution mode", "Small scope.", "## Delivery Report", "v0.1 is ready."):
        assert expected in text
    assert "Implementation" in text and "Built T01 and T02." in text


def test_the_export_lists_the_blueprint_files() -> None:
    text = export(finished_values())
    assert "## Blueprint Pack" in text
    assert "- `PRD.md`" in text and "- `plan.md`" in text


def test_the_export_shows_each_verification_lane_with_its_failures() -> None:
    text = export(finished_values())
    assert "## Verification" in text
    assert "tests" in text and "quality" in text and "requirements" in text
    assert "ruff: 2 errors" in text and "`check quality`" in text
    assert "after 1 fix attempt" in text


def test_a_verification_that_has_not_been_delivered_yet_is_exported_too() -> None:
    values = finished_values(delivery_report="", stage="report")
    text = export(values)
    assert "## Verification" in text and "ruff: 2 errors" in text and "## Delivery Report" not in text


def test_a_verification_that_never_ran_exports_its_report() -> None:
    values = finished_values(verification={"passed": False, "attempts": 0, "report": "Budget used up.", "lanes": []})
    assert "Budget used up." in export(values)


def test_decisions_and_the_moderator_keep_their_own_headings() -> None:
    text = export(finished_values())
    assert "## Moderator" in text and "logging-first MVP" in text
    assert "## Your Decisions" in text and "Architecture choice: Option A" in text
    assert "## You" not in text.replace("## Your", "")  # no numbered "You 2", "You 3" sections


def test_the_markdown_export_keeps_what_it_always_had() -> None:
    text = export(finished_values())
    for expected in ("# Idea-to-MVP Session Export", "## Idea", "## Panel Discussion", "## Summary", "## MVP Questions"):
        assert expected in text
    assert "## Your Answers" in text and "## Architect Output" in text


def test_the_json_export_carries_the_same_run_in_structured_form() -> None:
    data = json.loads(
        build_session_json(entries=transcript_from_state(finished_values(), None), thread_id="t1", mode="done", saved_at=SAVED_AT)
    )
    assert data["thread_id"] == "t1" and data["stage"] == "done" and data["saved_at"].startswith("2026-09-30")
    assert data["idea"] == "Climbing log app"
    assert data["blueprint_files"] == ["PRD.md", "ARCHITECTURE.md", "plan.md"]
    assert data["verification"]["attempts"] == 1
    assert [item["lane"] for item in data["verification"]["lanes"]] == ["tests", "quality", "requirements"]
    assert data["delivery_report"].startswith("## Delivery report")
    kinds = [entry["kind"] for entry in data["entries"]]
    assert kinds[0] == "idea" and "strategy" in kinds and kinds[-1] == "delivery_report"
    assert all(set(entry) == {"kind", "speaker", "content"} for entry in data["entries"])  # nothing unserialisable


def test_the_json_export_of_a_short_session_has_no_later_stages() -> None:
    data = json.loads(
        build_session_json(entries=[ViewEntry("idea", "You", "An idea")], thread_id="", mode="idea", saved_at=SAVED_AT)
    )
    assert data["blueprint_files"] == [] and data["verification"] is None and data["delivery_report"] == ""


def test_saving_writes_both_formats_side_by_side(tmp_path: Path) -> None:
    markdown, json_path = save_session_export(
        exports_dir=tmp_path / "exports",
        entries=[ViewEntry("idea", "You", "An idea")],
        thread_id="t1",
        mode="idea",
    )
    assert markdown.suffix == ".md" and json_path.suffix == ".json" and markdown.stem == json_path.stem
    assert markdown.read_text(encoding="utf-8").startswith("# Idea-to-MVP Session Export")
    assert json.loads(json_path.read_text(encoding="utf-8"))["idea"] == "An idea"


def test_the_export_keeps_the_stated_preferences_next_to_the_idea() -> None:
    values = finished_values(preferences={"platform": "web", "stack_hints": "TypeScript + Postgres"})
    text = export(values)
    assert "## Project Preferences" in text and "TypeScript + Postgres" in text
    assert text.index("## Idea") < text.index("## Project Preferences") < text.index("## Panel Discussion")
    assert "## Project Preferences" not in export(finished_values())
    data = json.loads(
        build_session_json(entries=transcript_from_state(values, None), thread_id="t", mode="done", saved_at=SAVED_AT)
    )
    assert "TypeScript + Postgres" in data["preferences"]

"""`scripts/make_example.py`: one finished run becomes a folder under `examples/` that is safe to publish."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from idea_to_mvp.config import get_settings

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "make_example.py"


def load_script():
    spec = importlib.util.spec_from_file_location("make_example", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_a_demo_run_becomes_a_complete_example(demo_env: Path, tmp_path: Path) -> None:
    script = load_script()
    target = tmp_path / "examples"
    screenshot = tmp_path / "shot.png"
    screenshot.write_bytes(b"\x89PNG\r\n")

    folder = await script.make_demo_example(
        "a habit tracker",
        "habit-tracker",
        examples_dir=target,
        screenshot=screenshot,
        settings=get_settings(),
    )

    assert folder == target / "habit-tracker"
    assert (folder / "blueprint" / "PRD.md").is_file() and (
        folder / "blueprint" / "plan.json"
    ).is_file()
    assert (folder / "DELIVERY_REPORT.md").read_text(encoding="utf-8").strip()
    assert (folder / "dashboard.png").read_bytes() == screenshot.read_bytes()
    session = json.loads((folder / "session.json").read_text(encoding="utf-8"))
    assert session["idea"] == "a habit tracker" and session["delivery_report"]
    assert "a habit tracker" in (folder / "session.md").read_text(encoding="utf-8")
    readme = (folder / "README.md").read_text(encoding="utf-8")
    assert "demo run" in readme and "a habit tracker" in readme and "passed" in readme
    assert (
        "tests passed" in readme and "?" not in readme.split("Verification", 1)[1].split("\n")[0]
    )  # lanes are named
    # nothing of the orchestrator's own records is published
    assert not list(folder.rglob(".idea-to-mvp")) and not list(folder.rglob(".git"))


async def test_an_example_is_not_overwritten_unless_asked(demo_env: Path, tmp_path: Path) -> None:
    script = load_script()
    settings = get_settings()
    first = await script.make_demo_example(
        "a habit tracker", "habits", examples_dir=tmp_path, settings=settings
    )
    marker = first / "keep.txt"
    marker.write_text("mine", encoding="utf-8")
    with pytest.raises(script.ExampleError, match="already exists"):
        await script.make_demo_example(
            "a habit tracker", "habits", examples_dir=tmp_path, settings=settings
        )
    assert marker.exists()
    await script.make_demo_example(
        "a habit tracker", "habits", examples_dir=tmp_path, settings=settings, force=True
    )
    assert not marker.exists()


def test_a_note_lands_in_the_example_readme() -> None:
    script = load_script()
    text = script._readme(
        {"user_idea": "x", "iteration": 1, "usage": []},
        source="live",
        name="n",
        has_screenshot=False,
        note="Profile fast.",
    )
    assert "Profile fast." in text


def test_the_example_name_is_a_plain_folder_name() -> None:
    script = load_script()
    for bad in ("", "../x", "a/b", ".hidden", "Has Space"):
        with pytest.raises(script.ExampleError, match="name"):
            script.check_name(bad)
    assert script.check_name("climbing-log") == "climbing-log"


def test_text_that_looks_like_a_key_stops_the_export(tmp_path: Path) -> None:
    script = load_script()
    folder = tmp_path / "out"
    folder.mkdir()
    (folder / "notes.md").write_text("token: sk-" + "a" * 40, encoding="utf-8")
    with pytest.raises(script.ExampleError, match=r"notes\.md"):
        script.check_for_secrets(folder, script.Forbidden())
    (folder / "notes.md").write_text("nothing to see", encoding="utf-8")
    script.check_for_secrets(folder, script.Forbidden())

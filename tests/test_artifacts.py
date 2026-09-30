"""The artifacts panel: a read-only file viewer that cannot leave the workspace, and the blueprint zip."""

import zipfile
from pathlib import Path

import pytest

from idea_to_mvp.ui.artifacts import (
    MAX_VIEW_BYTES,
    artifact_key,
    blueprint_zip,
    read_workspace_file,
)


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "projects" / "ws"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('hi')\n")
    (root / "README.md").write_text("# App\n")
    return root


def test_a_file_in_the_workspace_is_shown_with_its_language(workspace: Path) -> None:
    view = read_workspace_file(workspace, "src/app.py")
    assert view.text == "print('hi')\n" and view.language == "python" and view.note == ""
    assert read_workspace_file(workspace, "README.md").language == "markdown"


def test_an_absolute_path_inside_the_workspace_is_accepted(workspace: Path) -> None:
    assert read_workspace_file(workspace, str(workspace / "src" / "app.py")).text == "print('hi')\n"


def test_nothing_outside_the_workspace_can_be_read(workspace: Path, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("outside")
    for attempt in ("../../secret.txt", str(secret), "src/../../../secret.txt", "/etc/hosts"):
        view = read_workspace_file(workspace, attempt)
        assert view.text == "" and view.note, attempt


def test_a_symlink_pointing_out_of_the_workspace_is_not_followed(workspace: Path, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("outside")
    (workspace / "link.txt").symlink_to(secret)
    (workspace / "linked").symlink_to(tmp_path, target_is_directory=True)
    assert read_workspace_file(workspace, "link.txt").text == ""
    assert read_workspace_file(workspace, "linked/secret.txt").text == ""


def test_secret_files_and_git_internals_are_hidden(workspace: Path) -> None:
    (workspace / ".env").write_text("KEY=secret")
    (workspace / ".env.local").write_text("KEY=secret")
    (workspace / ".env.example").write_text("KEY=")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("[core]")
    assert read_workspace_file(workspace, ".env").text == "" and read_workspace_file(workspace, ".env.local").text == ""
    assert read_workspace_file(workspace, ".git/config").text == ""
    assert read_workspace_file(workspace, ".env.example").text == "KEY="


def test_large_and_binary_files_are_described_instead_of_shown(workspace: Path) -> None:
    (workspace / "big.txt").write_text("x" * (MAX_VIEW_BYTES + 1))
    (workspace / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff\xfe")
    assert read_workspace_file(workspace, "big.txt").text == "" and "large" in read_workspace_file(workspace, "big.txt").note
    assert read_workspace_file(workspace, "logo.png").text == "" and "binary" in read_workspace_file(workspace, "logo.png").note


def test_missing_files_directories_and_empty_selections_are_handled(workspace: Path) -> None:
    for selection in ("nope.txt", "src", "", None, []):
        assert read_workspace_file(workspace, selection).text == ""  # type: ignore[arg-type]
    assert read_workspace_file(workspace / "gone", "x").text == ""


def test_a_selection_may_be_a_list_as_the_file_explorer_returns_it(workspace: Path) -> None:
    assert read_workspace_file(workspace, ["src/app.py"]).text == "print('hi')\n"  # type: ignore[arg-type]


# ------------------------------------------------------------------ blueprint


@pytest.fixture()
def bundle(tmp_path: Path) -> Path:
    root = tmp_path / "blueprints" / "20260101-000000-000000-idea"
    root.mkdir(parents=True)
    (root / "PRD.md").write_text("# PRD")
    (root / "plan.md").write_text("# plan")
    return root


def test_the_blueprint_folder_is_zipped_for_download(bundle: Path, tmp_path: Path) -> None:
    archive = blueprint_zip(bundle, tmp_path / "deliveries")
    assert archive.name == f"{bundle.name}-blueprint.zip"
    with zipfile.ZipFile(archive) as bundle_zip:
        assert {f"{bundle.name}/PRD.md", f"{bundle.name}/plan.md"} <= set(bundle_zip.namelist())


def test_the_artifact_key_changes_when_the_blueprint_is_edited(bundle: Path, tmp_path: Path) -> None:
    before = artifact_key(str(tmp_path / "ws"), str(bundle), "")
    assert artifact_key(str(tmp_path / "ws"), str(bundle), "") == before
    import os
    import time

    later = time.time() + 5
    os.utime(bundle / "PRD.md", (later, later))
    assert artifact_key(str(tmp_path / "ws"), str(bundle), "") != before
    assert artifact_key(str(tmp_path / "ws"), str(bundle), "a.zip") != artifact_key(str(tmp_path / "ws"), str(bundle), "b.zip")

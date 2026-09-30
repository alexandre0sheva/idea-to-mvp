"""The delivery bundle: a zip of the project without dependencies or secrets, and a git tag per version."""

import subprocess
import zipfile
from pathlib import Path

import pytest

from idea_to_mvp.delivery import make_delivery_zip, tag_iteration
from idea_to_mvp.implementation.workspace import commit_workspace, prepare_workspace


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    bundle = tmp_path / "blueprints" / "20260101-000000-000000-idea"
    bundle.mkdir(parents=True)
    (bundle / "README.md").write_text("# idea")
    ws = prepare_workspace(bundle, tmp_path / "projects")
    (ws / "src").mkdir()
    (ws / "src" / "app.py").write_text("print('hi')\n")
    return ws


def names(archive: Path) -> set[str]:
    with zipfile.ZipFile(archive) as bundle:
        return set(bundle.namelist())


def git(workspace: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=workspace, capture_output=True, text=True, check=True).stdout.strip()


# ------------------------------------------------------------------------- zip


def test_the_zip_holds_the_project_under_its_own_folder(workspace: Path, tmp_path: Path) -> None:
    archive = make_delivery_zip(workspace, tmp_path / "deliveries")
    assert archive.parent == tmp_path / "deliveries" and archive.suffix == ".zip"
    assert {f"{workspace.name}/README.md", f"{workspace.name}/src/app.py", f"{workspace.name}/.gitignore"} <= names(archive)


@pytest.mark.parametrize(
    "excluded",
    ["node_modules/pkg/index.js", ".venv/bin/python", "venv/lib/x.py", "coverage/lcov.info", "dist/app.js", "src/__pycache__/app.pyc",
     ".pytest_cache/v/cache", ".idea-to-mvp/progress.json"],
)
def test_dependencies_build_output_and_orchestrator_files_are_left_out(workspace: Path, tmp_path: Path, excluded: str) -> None:
    path = workspace / excluded
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    assert f"{workspace.name}/{excluded}" not in names(make_delivery_zip(workspace, tmp_path / "out"))


def test_secrets_are_never_zipped_but_the_example_env_file_is(workspace: Path, tmp_path: Path) -> None:
    (workspace / ".env").write_text("API_KEY=secret")
    (workspace / ".env.local").write_text("API_KEY=secret")
    (workspace / ".env.example").write_text("API_KEY=")
    found = names(make_delivery_zip(workspace, tmp_path / "out"))
    assert f"{workspace.name}/.env.example" in found
    assert f"{workspace.name}/.env" not in found and f"{workspace.name}/.env.local" not in found


def test_symlinks_are_skipped_so_the_zip_cannot_leak_files_from_outside_the_project(workspace: Path, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("outside")
    (workspace / "link.txt").symlink_to(secret)
    (workspace / "linked_dir").symlink_to(tmp_path)
    found = names(make_delivery_zip(workspace, tmp_path / "out"))
    assert not any("link" in name for name in found)


def test_the_git_history_is_left_out_unless_asked_for(workspace: Path, tmp_path: Path) -> None:
    assert not any("/.git/" in name for name in names(make_delivery_zip(workspace, tmp_path / "a")))
    assert any(name.startswith(f"{workspace.name}/.git/") for name in names(make_delivery_zip(workspace, tmp_path / "b", include_git=True)))


def test_each_version_gets_its_own_zip_and_a_rerun_updates_it_in_place(workspace: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    v1 = make_delivery_zip(workspace, out, label="v0.1")
    (workspace / "src" / "new.py").write_text("x = 1\n")
    v2 = make_delivery_zip(workspace, out, label="v0.2")
    assert v1 != v2 and v1.name.endswith("-v0.1.zip") and v2.name.endswith("-v0.2.zip")
    assert f"{workspace.name}/src/new.py" not in names(v1) and f"{workspace.name}/src/new.py" in names(v2)
    (workspace / "src" / "newer.py").write_text("y = 2\n")
    assert make_delivery_zip(workspace, out, label="v0.2") == v2
    assert f"{workspace.name}/src/newer.py" in names(v2)
    assert sorted(p.name for p in out.iterdir()) == sorted([v1.name, v2.name])  # no temp files left behind


# ------------------------------------------------------------------------- tag


def test_tagging_an_iteration_marks_the_current_commit(workspace: Path) -> None:
    commit_workspace(workspace, "v1 work")
    assert tag_iteration(workspace, 1) == "v0.1"
    assert git(workspace, "rev-list", "-n1", "v0.1") == git(workspace, "rev-parse", "HEAD")


def test_retagging_moves_the_tag_to_the_latest_verified_state(workspace: Path) -> None:
    tag_iteration(workspace, 2)
    (workspace / "src" / "fix.py").write_text("z = 3\n")
    commit_workspace(workspace, "fix attempt")
    assert tag_iteration(workspace, 2) == "v0.2"
    assert git(workspace, "rev-list", "-n1", "v0.2") == git(workspace, "rev-parse", "HEAD")


def test_uncommitted_work_is_committed_before_it_is_tagged(workspace: Path) -> None:
    (workspace / "src" / "late.py").write_text("w = 4\n")
    tag_iteration(workspace, 3)
    assert "src/late.py" in git(workspace, "ls-tree", "-r", "--name-only", "v0.3").splitlines()


def test_a_folder_that_is_not_a_repository_gets_no_tag(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert tag_iteration(plain, 1) == ""

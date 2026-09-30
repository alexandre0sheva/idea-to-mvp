import subprocess
from pathlib import Path

import pytest

from idea_to_mvp.implementation import workspace as ws
from idea_to_mvp.implementation.workspace import commit_workspace, prepare_workspace, workspace_tree


@pytest.fixture(autouse=True)
def no_git_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The orchestrator must commit on machines with no git identity and ignore the user's config."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def bundle(tmp_path: Path) -> Path:
    root = tmp_path / "blueprints" / "20260101-000000-000000-my-idea"
    (root / ".claude" / "agents").mkdir(parents=True)
    (root / "plan.md").write_text("# plan")
    (root / ".claude" / "agents" / "backend-api.md").write_text("---\nname: backend-api\n---\n\nbody")
    return root


def git(workspace: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=workspace, capture_output=True, text=True, check=True
    ).stdout.strip()


# ------------------------------------------------------------------ preparing


def test_the_bundle_is_copied_into_a_fresh_workspace(bundle: Path, tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    workspace = prepare_workspace(bundle, projects)
    assert workspace.parent == projects and workspace.name == bundle.name
    assert (workspace / "plan.md").read_text() == "# plan"
    assert (workspace / ".claude" / "agents" / "backend-api.md").exists()


def test_a_second_workspace_for_the_same_bundle_does_not_collide(bundle: Path, tmp_path: Path) -> None:
    first = prepare_workspace(bundle, tmp_path / "projects")
    second = prepare_workspace(bundle, tmp_path / "projects")
    assert second != first and (second / "plan.md").exists() and (first / "plan.md").exists()


def test_heavy_and_generated_directories_are_not_copied(bundle: Path, tmp_path: Path) -> None:
    for name in ("node_modules", ".venv", "coverage", "dist", "__pycache__", ".git"):
        (bundle / name).mkdir()
        (bundle / name / "junk.txt").write_text("x")
        (bundle / "src" / name).mkdir(parents=True, exist_ok=True)  # also when nested
        (bundle / "src" / name / "junk.txt").write_text("x")
    (bundle / "src" / "app.py").write_text("print(1)")
    workspace = prepare_workspace(bundle, tmp_path / "projects")
    names = {p.name for p in workspace.rglob("*") if ".git" not in p.relative_to(workspace).parts[:1]}
    for excluded in ("node_modules", ".venv", "coverage", "dist", "__pycache__"):
        assert excluded not in names, excluded
    assert (workspace / "src" / "app.py").exists()
    assert "junk.txt" not in names


def test_symlinks_in_the_bundle_are_copied_as_links_not_followed(bundle: Path, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("secret")
    (bundle / "leak").symlink_to(secret)
    workspace = prepare_workspace(bundle, tmp_path / "projects")
    assert (workspace / "leak").is_symlink()


# --------------------------------------------------------------------- git


def test_the_workspace_is_a_git_repository_with_one_blueprint_commit_and_a_clean_tree(
    bundle: Path, tmp_path: Path
) -> None:
    workspace = prepare_workspace(bundle, tmp_path / "projects")
    assert (workspace / ".git").is_dir()
    assert git(workspace, "log", "--format=%s").splitlines() == ["blueprint"]
    assert git(workspace, "status", "--porcelain") == ""
    assert {"plan.md", ".gitignore", ".claude/agents/backend-api.md"} <= set(git(workspace, "ls-files").splitlines())


def test_the_gitignore_covers_dependencies_caches_and_secrets(bundle: Path, tmp_path: Path) -> None:
    workspace = prepare_workspace(bundle, tmp_path / "projects")
    lines = set((workspace / ".gitignore").read_text().splitlines())
    for entry in ("node_modules/", ".venv/", "__pycache__/", "coverage/", "dist/", ".env", ".pytest_cache/", ".DS_Store"):
        assert entry in lines, entry
    assert "!.env.example" in lines


def test_an_existing_gitignore_is_kept_and_extended(bundle: Path, tmp_path: Path) -> None:
    (bundle / ".gitignore").write_text("custom-output/\n")
    workspace = prepare_workspace(bundle, tmp_path / "projects")
    lines = (workspace / ".gitignore").read_text().splitlines()
    assert lines[0] == "custom-output/" and "node_modules/" in lines


def test_later_commits_are_made_by_the_orchestrator_and_skip_ignored_files(bundle: Path, tmp_path: Path) -> None:
    workspace = prepare_workspace(bundle, tmp_path / "projects")
    (workspace / "src").mkdir()
    (workspace / "src" / "app.py").write_text("print(1)")
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules" / "dep.js").write_text("x")
    (workspace / ".env").write_text("SECRET=1")
    assert commit_workspace(workspace, "implementation") is True
    assert git(workspace, "log", "--format=%s").splitlines() == ["implementation", "blueprint"]
    tracked = set(git(workspace, "ls-files").splitlines())
    assert "src/app.py" in tracked and not any(p.startswith("node_modules") or p == ".env" for p in tracked)
    assert commit_workspace(workspace, "nothing changed") is False  # clean tree: no empty commit


def test_committing_outside_a_git_repository_is_a_quiet_no_op(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert commit_workspace(plain, "x") is False
    assert commit_workspace(tmp_path / "missing", "x") is False


def test_a_missing_git_binary_does_not_break_workspace_preparation(
    bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(ws, "GIT", "git-that-does-not-exist")
    workspace = prepare_workspace(bundle, tmp_path / "projects")
    assert (workspace / "plan.md").exists() and not (workspace / ".git").exists()
    assert "git" in caplog.text.lower()


# ---------------------------------------- a hostile repo must not run code in the orchestrator


def test_a_planted_fsmonitor_hook_or_filter_never_runs_when_the_orchestrator_commits(
    bundle: Path, tmp_path: Path
) -> None:
    workspace = prepare_workspace(bundle, tmp_path / "projects")
    marker = tmp_path / "pwned"
    # What a malicious agent could leave behind for the orchestrator's next git command.
    config = workspace / ".git" / "config"
    config.write_text(
        config.read_text()
        + f"[core]\n\tfsmonitor = touch {marker}.fsmonitor\n"
        + f"[filter \"evil\"]\n\tclean = touch {marker}.filter\n\tsmudge = cat\n"
    )
    (workspace / ".gitattributes").write_text("* filter=evil\n")
    hook = workspace / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(f"#!/bin/sh\ntouch {marker}.hook\n")
    hook.chmod(0o755)
    (workspace / "new.txt").write_text("change")
    commit_workspace(workspace, "implementation")
    assert not list(tmp_path.glob("pwned*")), [p.name for p in tmp_path.glob("pwned*")]


def test_a_git_entry_that_is_not_a_plain_directory_is_not_trusted(bundle: Path, tmp_path: Path) -> None:
    workspace = prepare_workspace(bundle, tmp_path / "projects")
    elsewhere = tmp_path / "elsewhere.git"
    (workspace / ".git").rename(elsewhere)
    (workspace / ".git").symlink_to(elsewhere, target_is_directory=True)
    (workspace / "new.txt").write_text("x")
    assert commit_workspace(workspace, "implementation") is False
    assert git(elsewhere, "log", "--format=%s").splitlines() == ["blueprint"]  # untouched


# -------------------------------------------------------------- workspace_tree


def test_workspace_tree_lists_project_files_and_hides_noise(tmp_path: Path) -> None:
    root = tmp_path / "w"
    for path in ("src/app.py", "README.md", "node_modules/dep/index.js", ".git/HEAD", ".venv/bin/python", "a/__pycache__/x.pyc"):
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text("x")
    tree = workspace_tree(root)
    assert "- `src/app.py`" in tree and "- `README.md`" in tree
    for hidden in ("node_modules", ".git", ".venv", "__pycache__"):
        assert hidden not in tree


def test_workspace_tree_truncates_and_handles_missing_or_empty_workspaces(tmp_path: Path) -> None:
    root = tmp_path / "w"
    root.mkdir()
    assert workspace_tree(root) == "- (empty workspace)"
    assert workspace_tree(tmp_path / "missing") == "- (workspace not found)"
    for index in range(5):
        (root / f"f{index}.txt").write_text("x")
    tree = workspace_tree(root, limit=3)
    assert tree.count("\n") == 3 and tree.endswith("- ... (truncated)")


# ---------------------------------------------------------------- diff stat


def _git(workspace: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(["git", *args], cwd=workspace, capture_output=True, text=True, check=True).stdout.strip()


def test_diff_stat_summarises_what_a_commit_changed(tmp_path: Path) -> None:
    from idea_to_mvp.implementation.workspace import commit_workspace, diff_stat, head_commit

    bundle = tmp_path / "blueprints" / "20260101-000000-000000-idea"
    bundle.mkdir(parents=True)
    (bundle / "README.md").write_text("# idea\n")
    workspace = prepare_workspace(bundle, tmp_path / "projects")
    (workspace / "app.py").write_text("print('hi')\nprint('there')\n")
    commit_workspace(workspace, "T01: app")
    commit = head_commit(workspace)
    assert commit is not None
    stat = diff_stat(workspace, commit)
    assert "app.py" in stat and "2 insertion" in stat and "1 file changed" in stat


def test_diff_stat_of_a_merge_shows_what_the_merge_brought_in(tmp_path: Path) -> None:
    from idea_to_mvp.implementation.workspace import commit_workspace, diff_stat, head_commit

    bundle = tmp_path / "blueprints" / "20260101-000000-000000-idea"
    bundle.mkdir(parents=True)
    (bundle / "README.md").write_text("# idea\n")
    workspace = prepare_workspace(bundle, tmp_path / "projects")
    base = _git(workspace, "rev-parse", "HEAD")
    _git(workspace, "checkout", "-q", "-b", "task/T02")
    (workspace / "left.py").write_text("x = 1\n")
    commit_workspace(workspace, "T02: left")
    _git(workspace, "checkout", "-q", "main")
    (workspace / "other.py").write_text("y = 2\n")
    commit_workspace(workspace, "T03: other")
    _git(workspace, "-c", "user.name=t", "-c", "user.email=t@t", "merge", "--no-ff", "-q", "-m", "merge T02", "task/T02")
    commit = head_commit(workspace)
    assert commit is not None and base
    stat = diff_stat(workspace, commit)
    assert "left.py" in stat and "other.py" not in stat  # the merge brought in T02's file, not main's own


def test_diff_stat_of_the_first_commit_falls_back_to_showing_it(tmp_path: Path) -> None:
    from idea_to_mvp.implementation.workspace import diff_stat, head_commit

    bundle = tmp_path / "blueprints" / "20260101-000000-000000-idea"
    bundle.mkdir(parents=True)
    (bundle / "README.md").write_text("# idea\n")
    workspace = prepare_workspace(bundle, tmp_path / "projects")
    first = head_commit(workspace)
    assert first is not None and "README.md" in diff_stat(workspace, first)


def test_diff_stat_refuses_anything_that_is_not_a_plain_commit_id(tmp_path: Path) -> None:
    from idea_to_mvp.implementation.workspace import diff_stat

    bundle = tmp_path / "blueprints" / "20260101-000000-000000-idea"
    bundle.mkdir(parents=True)
    (bundle / "README.md").write_text("# idea\n")
    workspace = prepare_workspace(bundle, tmp_path / "projects")
    for bad in ("--help", "HEAD~1", "abc;rm -rf /", "", "zzzz", "--output=/tmp/x"):
        assert diff_stat(workspace, bad) == ""
    plain = tmp_path / "plain"
    plain.mkdir()
    assert diff_stat(plain, "abcd1234") == ""

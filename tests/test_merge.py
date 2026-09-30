"""Worktrees and merges on real temporary git repositories."""

import subprocess
from pathlib import Path

import pytest

from idea_to_mvp.implementation import merge
from idea_to_mvp.implementation.workspace import prepare_workspace


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
    (bundle / "README.md").write_text("line one\nline two\nline three\n")
    (bundle / "app.py").write_text("print('base')\n")
    return prepare_workspace(bundle, tmp_path / "projects")


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def commit_in(workspace: Path, worktree: Path, task_id: str, files: dict[str, str]) -> str | None:
    for name, content in files.items():
        (worktree / name).parent.mkdir(parents=True, exist_ok=True)
        (worktree / name).write_text(content)
    return merge.commit_worktree(workspace, worktree, task_id, f"{task_id}: work")


async def test_a_worktree_is_a_sibling_checkout_on_its_own_branch(workspace: Path) -> None:
    worktree = await merge.create_worktree(workspace, "T02")
    assert worktree == merge.worktree_path(workspace, "T02")
    assert worktree.parent == workspace.parent / ".worktrees" / workspace.name  # next to the workspace, not inside it
    assert (worktree / "README.md").read_text().startswith("line one")
    assert (worktree / ".git").is_file()
    assert git(worktree, "branch", "--show-current") == "task/T02"
    assert not any(workspace.rglob(".worktrees"))


async def test_a_stale_worktree_from_a_killed_run_is_replaced_from_the_current_head(workspace: Path) -> None:
    stale = await merge.create_worktree(workspace, "T02")
    (stale / "leftover.txt").write_text("half done")
    commit_in(workspace, stale, "T02", {"old.txt": "old attempt"})
    (workspace / "later.txt").write_text("landed on main meanwhile")
    git(workspace, "add", "-A")
    git(workspace, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "main moved on")
    fresh = await merge.create_worktree(workspace, "T02")
    assert fresh == stale and not (fresh / "leftover.txt").exists() and not (fresh / "old.txt").exists()
    assert (fresh / "later.txt").exists()  # recreated from where main is now


async def test_commit_worktree_commits_on_the_task_branch_and_not_on_main(workspace: Path) -> None:
    worktree = await merge.create_worktree(workspace, "T02")
    main_head = git(workspace, "rev-parse", "HEAD")
    sha = commit_in(workspace, worktree, "T02", {"a.txt": "a"})
    assert sha and git(workspace, "rev-parse", "--short", "task/T02") == sha
    assert git(workspace, "rev-parse", "HEAD") == main_head and not (workspace / "a.txt").exists()
    assert git(workspace, "log", "-1", "--format=%s", "task/T02") == "T02: work"


async def test_committing_nothing_returns_none(workspace: Path) -> None:
    worktree = await merge.create_worktree(workspace, "T02")
    assert merge.commit_worktree(workspace, worktree, "T02", "T02: nothing") is None


async def test_a_worktree_whose_git_file_was_rewritten_cannot_redirect_the_orchestrator(
    workspace: Path, tmp_path: Path
) -> None:
    worktree = await merge.create_worktree(workspace, "T02")
    other = tmp_path / "other-repo"
    other.mkdir()
    git(other, "init", "-q")
    (worktree / ".git").write_text(f"gitdir: {other / '.git'}\n")  # what a malicious agent could do
    sha = commit_in(workspace, worktree, "T02", {"a.txt": "a"})
    assert sha and git(workspace, "rev-parse", "--short", "task/T02") == sha  # landed on the trusted task branch
    assert subprocess.run(["git", "log"], cwd=other, capture_output=True).returncode != 0  # the other repo: no commits


# ------------------------------------------------------------------------- merging


async def test_branches_touching_different_files_merge_cleanly(workspace: Path) -> None:
    left = await merge.create_worktree(workspace, "T02")
    right = await merge.create_worktree(workspace, "T03")
    commit_in(workspace, left, "T02", {"left.txt": "left"})
    commit_in(workspace, right, "T03", {"right.txt": "right"})

    first = await merge.merge_task(workspace, "T02", message="T02: Left (merged)")
    second = await merge.merge_task(workspace, "T03", message="T03: Right (merged)")

    assert (first.status, second.status) == ("merged", "merged")
    assert first.commit and second.commit and second.commit == git(workspace, "rev-parse", "--short", "HEAD")
    assert (workspace / "left.txt").read_text() == "left" and (workspace / "right.txt").read_text() == "right"
    subjects = git(workspace, "log", "--format=%s").splitlines()
    assert {"T02: work", "T03: work", "T03: Right (merged)"} <= set(subjects)  # the second needed a real merge commit
    assert git(workspace, "status", "--porcelain") == ""


async def test_a_branch_that_can_fast_forward_leaves_a_linear_history(workspace: Path) -> None:
    worktree = await merge.create_worktree(workspace, "T02")
    commit_in(workspace, worktree, "T02", {"a.txt": "a"})
    outcome = await merge.merge_task(workspace, "T02", message="ignored for a fast-forward")
    assert outcome.status == "merged"
    assert git(workspace, "log", "--format=%s").splitlines()[:2] == ["T02: work", "blueprint"]


async def test_the_same_line_changed_on_both_sides_is_reported_as_a_conflict(workspace: Path) -> None:
    left = await merge.create_worktree(workspace, "T02")
    right = await merge.create_worktree(workspace, "T03")
    commit_in(workspace, left, "T02", {"README.md": "line one\nLEFT\nline three\n"})
    commit_in(workspace, right, "T03", {"README.md": "line one\nRIGHT\nline three\n"})
    assert (await merge.merge_task(workspace, "T02", message="m")).status == "merged"
    outcome = await merge.merge_task(workspace, "T03", message="m")
    assert outcome.status == "conflict" and "README.md" in outcome.detail and outcome.commit is None
    assert merge.conflicted_files(workspace) == ["README.md"]
    assert "<<<<<<<" in (workspace / "README.md").read_text()  # left in place for a resolver


async def test_aborting_a_conflicted_merge_restores_the_workspace(workspace: Path) -> None:
    left = await merge.create_worktree(workspace, "T02")
    right = await merge.create_worktree(workspace, "T03")
    commit_in(workspace, left, "T02", {"README.md": "line one\nLEFT\nline three\n"})
    commit_in(workspace, right, "T03", {"README.md": "line one\nRIGHT\nline three\n"})
    await merge.merge_task(workspace, "T02", message="m")
    head = git(workspace, "rev-parse", "HEAD")
    await merge.merge_task(workspace, "T03", message="m")
    await merge.abort_merge(workspace)
    assert git(workspace, "rev-parse", "HEAD") == head and git(workspace, "status", "--porcelain") == ""
    assert (workspace / "README.md").read_text() == "line one\nLEFT\nline three\n"
    assert merge.conflicted_files(workspace) == []


async def _conflicted(workspace: Path) -> None:
    left = await merge.create_worktree(workspace, "T02")
    right = await merge.create_worktree(workspace, "T03")
    commit_in(workspace, left, "T02", {"README.md": "line one\nLEFT\nline three\n"})
    commit_in(workspace, right, "T03", {"README.md": "line one\nRIGHT\nline three\n"})
    await merge.merge_task(workspace, "T02", message="m")
    assert (await merge.merge_task(workspace, "T03", message="m")).status == "conflict"


async def test_a_resolved_conflict_is_completed_with_a_merge_commit(workspace: Path) -> None:
    await _conflicted(workspace)
    (workspace / "README.md").write_text("line one\nLEFT and RIGHT\nline three\n")
    outcome = await merge.complete_merge(workspace, "T03: Right (conflicts resolved)", "T03")
    assert outcome.status == "merged" and outcome.commit == git(workspace, "rev-parse", "--short", "HEAD")
    assert git(workspace, "log", "-1", "--format=%s") == "T03: Right (conflicts resolved)"
    assert git(workspace, "status", "--porcelain") == ""


async def test_leftover_conflict_markers_are_not_accepted(workspace: Path) -> None:
    await _conflicted(workspace)  # the resolver "fixed" nothing
    outcome = await merge.complete_merge(workspace, "msg", "T03")
    assert outcome.status == "conflict" and "README.md" in outcome.detail
    (workspace / "README.md").write_text("line one\n<<<<<<< HEAD\nstill here\nline three\n")  # staged-looking but marked
    git(workspace, "add", "README.md")
    assert (await merge.complete_merge(workspace, "msg", "T03")).status == "conflict"


async def test_a_resolver_that_committed_the_merge_itself_is_recognised(workspace: Path) -> None:
    await _conflicted(workspace)
    (workspace / "README.md").write_text("line one\nBOTH\nline three\n")
    git(workspace, "add", "-A")
    git(workspace, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "resolved by the agent")
    outcome = await merge.complete_merge(workspace, "msg", "T03")
    assert outcome.status == "merged"


async def test_a_planted_fsmonitor_in_the_main_config_does_not_run_during_a_merge(
    workspace: Path, tmp_path: Path
) -> None:
    worktree = await merge.create_worktree(workspace, "T02")
    commit_in(workspace, worktree, "T02", {"a.txt": "a"})
    marker = tmp_path / "pwned"
    config = workspace / ".git" / "config"
    config.write_text(config.read_text() + f"[core]\n\tfsmonitor = touch {marker}\n")
    await merge.merge_task(workspace, "T02", message="m")
    assert not marker.exists()


# ---------------------------------------------------------------------- cleaning up


async def test_removing_a_worktree_deletes_its_directory_and_branch_and_is_idempotent(workspace: Path) -> None:
    worktree = await merge.create_worktree(workspace, "T02")
    commit_in(workspace, worktree, "T02", {"a.txt": "a"})
    await merge.remove_worktree(workspace, "T02")
    assert not worktree.exists()
    assert subprocess.run(["git", "rev-parse", "--verify", "task/T02"], cwd=workspace, capture_output=True).returncode != 0
    assert "T02" not in git(workspace, "worktree", "list")
    await merge.remove_worktree(workspace, "T02")  # nothing left: still fine


async def test_the_shared_worktree_folder_disappears_when_the_last_worktree_does(workspace: Path) -> None:
    await merge.create_worktree(workspace, "T02")
    await merge.create_worktree(workspace, "T03")
    await merge.remove_worktree(workspace, "T02")
    assert merge.worktree_path(workspace, "T03").exists()
    await merge.remove_worktree(workspace, "T03")
    assert not (workspace.parent / ".worktrees").exists()

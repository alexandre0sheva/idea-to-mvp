"""Git worktrees for parallel tasks, and merging their branches back deterministically.

Each parallel task gets its own worktree and branch (`task/<id>`) beside the workspace, in
`<projects>/.worktrees/<workspace>/<id>`, so its agent session (whose sandbox root and tool guard are that
folder) cannot touch the main checkout or another task. The orchestrator commits the task's work on its
branch and merges the branches into the workspace in task-id order.

The agent can rewrite the `.git` file of its worktree, so the orchestrator never trusts it: commits are
made with `--git-dir` pinned to the worktree's administrative directory inside the main repository (which
the agent cannot reach), and the main repository's config is reset before every merge (`workspace.py`).
"""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from idea_to_mvp.implementation.workspace import harden_git_dir, head_commit, run_git

WORKTREES_DIR = ".worktrees"
_MARKER_PATTERN = r"^(<{7}|>{7})( |$)"


@dataclass(frozen=True)
class MergeOutcome:
    status: Literal["merged", "conflict"]
    detail: str
    commit: str | None = None


def worktree_root(workspace: Path) -> Path:
    workspace = Path(workspace)
    return workspace.parent / WORKTREES_DIR / workspace.name


def worktree_path(workspace: Path, task_id: str) -> Path:
    return worktree_root(workspace) / task_id


def branch_name(task_id: str) -> str:
    return f"task/{task_id}"


def _admin_dir(workspace: Path, task_id: str) -> Path:
    return Path(workspace) / ".git" / "worktrees" / task_id


def _git_ok(workspace: Path, *args: str) -> bool:
    done = run_git(workspace, *args)
    return done is not None and done.returncode == 0


# ---------------------------------------------------------------------------- cleanup


def _remove_worktree(workspace: Path, task_id: str) -> None:
    path = worktree_path(workspace, task_id)
    if (Path(workspace) / ".git").is_dir():
        run_git(workspace, "worktree", "remove", "--force", str(path))
        shutil.rmtree(_admin_dir(workspace, task_id), ignore_errors=True)
        run_git(workspace, "worktree", "prune")
        run_git(workspace, "branch", "-D", branch_name(task_id))
    shutil.rmtree(path, ignore_errors=True)
    for folder in (worktree_root(workspace), worktree_root(workspace).parent):
        try:
            folder.rmdir()  # only when empty
        except OSError:
            break


def remove_all_worktrees(workspace: Path) -> None:
    root = worktree_root(Path(workspace))
    if root.is_dir():
        for child in sorted(root.iterdir()):
            _remove_worktree(workspace, child.name)
    shutil.rmtree(root, ignore_errors=True)
    try:
        root.parent.rmdir()
    except OSError:
        pass


async def remove_worktree(workspace: Path, task_id: str) -> None:
    await asyncio.to_thread(_remove_worktree, Path(workspace), task_id)


# ---------------------------------------------------------------------------- create


def _create_worktree(workspace: Path, task_id: str) -> Path:
    harden_git_dir(workspace / ".git")
    _remove_worktree(workspace, task_id)  # a half-finished attempt of a killed run is discarded
    path = worktree_path(workspace, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    done = run_git(workspace, "worktree", "add", "-q", "-b", branch_name(task_id), str(path))
    if done is None or done.returncode != 0:
        raise RuntimeError(f"git worktree add failed for {task_id}: {(done.stderr if done else 'git unavailable').strip()}")
    expected = os.path.realpath(_admin_dir(workspace, task_id))
    declared = (path / ".git").read_text(encoding="utf-8").strip().removeprefix("gitdir:").strip()
    if os.path.realpath(declared) != expected:
        raise RuntimeError(f"unexpected git dir for worktree {task_id}: {declared!r}")
    return path


async def create_worktree(workspace: Path, task_id: str) -> Path:
    """A fresh worktree and branch `task/<id>` from the workspace's current HEAD."""
    return await asyncio.to_thread(_create_worktree, Path(workspace), task_id)


# ---------------------------------------------------------------------- task commits


def commit_worktree(workspace: Path, work_dir: Path, task_id: str, message: str) -> str | None:
    """Commit the task's changes on its branch (short sha, or None when nothing changed)."""
    workspace, work_dir = Path(workspace), Path(work_dir)
    git_dir = _admin_dir(workspace, task_id)
    if not git_dir.is_dir() or not work_dir.is_dir():
        return None
    pinned = (f"--git-dir={git_dir}", f"--work-tree={work_dir}")
    if not _git_ok(work_dir, *pinned, "add", "-A"):
        return None
    staged = run_git(work_dir, *pinned, "diff", "--cached", "--quiet")
    if staged is None or staged.returncode == 0:
        return None
    if not _git_ok(work_dir, *pinned, "commit", "-q", "--no-verify", "--no-gpg-sign", "-m", message):
        return None
    shown = run_git(work_dir, *pinned, "rev-parse", "--short", "HEAD")
    return shown.stdout.strip() if shown is not None and shown.returncode == 0 else None


# ---------------------------------------------------------------------------- merging


def conflicted_files(workspace: Path) -> list[str]:
    done = run_git(Path(workspace), "diff", "--name-only", "--diff-filter=U")
    return sorted(done.stdout.split()) if done is not None and done.returncode == 0 else []


def _merge_task(workspace: Path, task_id: str, message: str | None) -> MergeOutcome:
    harden_git_dir(workspace / ".git")
    done = run_git(workspace, "merge", "--no-edit", "-m", message or f"merge {task_id}", branch_name(task_id))
    if done is not None and done.returncode == 0:
        return MergeOutcome("merged", done.stdout.strip()[-200:], head_commit(workspace))
    files = conflicted_files(workspace)
    if files:
        return MergeOutcome("conflict", "conflicts in: " + ", ".join(files))
    run_git(workspace, "merge", "--abort")
    raise RuntimeError(f"git merge {branch_name(task_id)} failed: {(done.stderr if done else 'git unavailable').strip()}")


async def merge_task(workspace: Path, task_id: str, *, message: str | None = None) -> MergeOutcome:
    """Merge the task's branch into the workspace. On conflict the merge is left in progress so a
    resolver can fix the files; finish it with `complete_merge` or undo it with `abort_merge`."""
    return await asyncio.to_thread(_merge_task, Path(workspace), task_id, message)


def _abort_merge(workspace: Path) -> None:
    if not _git_ok(workspace, "merge", "--abort"):
        run_git(workspace, "reset", "--merge")


async def abort_merge(workspace: Path) -> None:
    await asyncio.to_thread(_abort_merge, Path(workspace))


def _has_markers(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False  # deleted: a delete/modify conflict resolved by deleting
    return any(line.startswith(("<<<<<<< ", ">>>>>>> ")) or line in ("<<<<<<<", ">>>>>>>") for line in text.splitlines())


def _complete_merge(workspace: Path, message: str, task_id: str) -> MergeOutcome:
    if not (workspace / ".git" / "MERGE_HEAD").exists():
        # The resolver may have committed the merge itself.
        if not conflicted_files(workspace) and _git_ok(workspace, "merge-base", "--is-ancestor", branch_name(task_id), "HEAD"):
            return MergeOutcome("merged", "merge already committed", head_commit(workspace))
        return MergeOutcome("conflict", "the merge is no longer in progress and the task branch is not merged")
    # A resolver edits the files but need not `git add` them, so judge by the content, then stage it.
    still_marked = [name for name in conflicted_files(workspace) if _has_markers(workspace / name)]
    if still_marked:
        return MergeOutcome("conflict", "still unresolved: " + ", ".join(still_marked))
    if not _git_ok(workspace, "add", "-A"):
        return MergeOutcome("conflict", "could not stage the resolution")
    marked = run_git(workspace, "grep", "--cached", "-l", "-E", _MARKER_PATTERN)
    if marked is not None and marked.returncode == 0 and marked.stdout.strip():
        return MergeOutcome("conflict", "conflict markers remain in: " + ", ".join(marked.stdout.split()))
    if leftover := conflicted_files(workspace):
        return MergeOutcome("conflict", "still unmerged: " + ", ".join(leftover))
    if not _git_ok(workspace, "commit", "-q", "--no-verify", "--no-gpg-sign", "-m", message):
        return MergeOutcome("conflict", "could not commit the merge")
    return MergeOutcome("merged", "conflicts resolved", head_commit(workspace))


async def complete_merge(workspace: Path, message: str, task_id: str) -> MergeOutcome:
    """Commit a merge whose conflicts have been resolved, refusing leftover conflicts or markers."""
    return await asyncio.to_thread(_complete_merge, Path(workspace), message, task_id)

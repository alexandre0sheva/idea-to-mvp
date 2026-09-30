"""The delivery bundle: a zip of the finished project and a git tag for every verified version.

Both are made by the orchestrator, outside the agents' sandbox, from a tree an agent has been writing to, so
the zip is built defensively: dependencies, build output, orchestrator internals, and secret files are left
out, and symbolic links are skipped (following one would put a file from outside the project in the zip).
"""

from __future__ import annotations

import logging
import os
import zipfile
from pathlib import Path

from idea_to_mvp.implementation.workspace import IGNORED_DIRS, commit_workspace, run_git

LOGGER = logging.getLogger(__name__)

# Directory names skipped at any depth, on top of the workspace's own ignored directories.
_SKIPPED_DIRS = frozenset({*IGNORED_DIRS, ".idea-to-mvp", "htmlcov", "build"})
_SKIPPED_FILES = frozenset({".DS_Store", ".coverage"})
_SKIPPED_SUFFIXES = (".pyc", ".pyo")


def _is_secret_env(name: str) -> bool:
    return (name == ".env" or name.startswith(".env.")) and name != ".env.example"


def _skipped(directory: str) -> bool:
    return directory in _SKIPPED_DIRS or directory.endswith(".egg-info")


def _files(workspace: Path, include_git: bool) -> list[Path]:
    found: list[Path] = []
    for root, dirs, files in os.walk(workspace, followlinks=False):
        # Symlinked directories are listed in `dirs` but never descended into; drop them here so they are
        # neither walked nor zipped.
        dirs[:] = sorted(
            d
            for d in dirs
            if not (Path(root, d).is_symlink() or (_skipped(d) and not (include_git and d == ".git")))
        )
        for name in sorted(files):
            path = Path(root, name)
            if path.is_symlink() or name in _SKIPPED_FILES or name.endswith(_SKIPPED_SUFFIXES) or _is_secret_env(name):
                continue
            found.append(path)
    return found


def make_delivery_zip(workspace: Path, dest_dir: Path, *, label: str = "", include_git: bool = False) -> Path:
    """Zip the project into `dest_dir` as `<workspace>[-<label>].zip` (replaced when it already exists).

    The archive holds the project under a folder named like the workspace. The git history is left out
    unless `include_git`.
    """
    workspace = Path(workspace)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"{workspace.name}{f'-{label}' if label else ''}.zip"
    temporary = target.with_name(target.name + ".part")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in _files(workspace, include_git):
                archive.write(path, arcname=f"{workspace.name}/{path.relative_to(workspace).as_posix()}")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def tag_iteration(workspace: Path, iteration: int) -> str:
    """Tag the workspace's current state `v0.<iteration>` (moving an existing tag of that name).

    Work the agents left uncommitted is committed first so the tag covers everything that was verified.
    Returns the tag, or "" when the workspace is not a usable git repository.
    """
    workspace = Path(workspace)
    git_dir = workspace / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        return ""
    tag = f"v0.{iteration}"
    commit_workspace(workspace, tag)  # also hardens the repository's config and hooks
    done = run_git(workspace, "tag", "-f", tag)
    if done is None or done.returncode != 0:
        LOGGER.warning("Could not tag %s in %s: %s", tag, workspace, (done.stderr if done else "git unavailable").strip())
        return ""
    return tag

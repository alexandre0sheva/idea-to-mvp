"""Implementation workspaces: a git repository prepared by the orchestrator, not by the agents.

The blueprint bundle is copied (without dependency and build directories) into a fresh folder under
`projects_dir`, turned into a git repository with a `.gitignore` and a first commit named "blueprint".
Every later commit is made here, by the orchestrator, after an agent session ends.

The orchestrator runs git *outside* the agents' sandbox on a repository an agent has been writing to, so
git is run hardened: its hooks, fsmonitor, filters, and user/global configuration are neutralised and
the repository's own config is reset before each commit (a planted `core.fsmonitor`, hook, or filter
would otherwise run as the user). Git is optional: without it the workspace is simply not a repository.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

LOGGER = logging.getLogger(__name__)

GIT = "git"
# Not copied from the bundle (and, as names, hidden from file listings).
IGNORED_DIRS = (
    "node_modules",
    ".venv",
    "venv",
    "coverage",
    "dist",
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
)
GITIGNORE_ENTRIES = (
    "node_modules/",
    ".venv/",
    "venv/",
    "__pycache__/",
    "*.pyc",
    ".pytest_cache/",
    ".ruff_cache/",
    ".mypy_cache/",
    "coverage/",
    ".coverage",
    "htmlcov/",
    "dist/",
    "build/",
    "*.egg-info/",
    ".env",
    ".env.*",
    "!.env.example",
    ".idea-to-mvp/",
    ".DS_Store",
)
_GIT_OPTIONS = (
    "core.fsmonitor=false",
    "core.hooksPath=/dev/null",
    "core.attributesFile=/dev/null",
    "commit.gpgsign=false",
    "protocol.ext.allow=never",
    "gc.auto=0",
    "user.name=idea-to-mvp",
    "user.email=idea-to-mvp@localhost",
    "init.defaultBranch=main",
)
_SAFE_GIT_CONFIG = "[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = false\n\tlogallrefupdates = true\n"
_PASSTHROUGH_ENV = ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")


def _git_env() -> dict[str, str]:
    env = {key: os.environ[key] for key in _PASSTHROUGH_ENV if key in os.environ}
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ATTR_NOSYSTEM": "1",
        }
    )
    return env


def run_git(workspace: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    """Run hardened git in `workspace`; None when git is not available."""
    command = [GIT]
    for option in _GIT_OPTIONS:
        command += ["-c", option]
    try:
        return subprocess.run(
            [*command, *args], cwd=workspace, env=_git_env(), capture_output=True, text=True, timeout=120, check=False
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        LOGGER.warning("git is not usable (%s); the workspace at %s is not version controlled.", exc, workspace)
        return None


def _write_gitignore(workspace: Path) -> None:
    path = workspace / ".gitignore"
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    missing = [entry for entry in GITIGNORE_ENTRIES if entry not in existing]
    path.write_text("\n".join([*existing, *missing]) + "\n", encoding="utf-8")


def _commit(workspace: Path, message: str) -> bool:
    if (added := run_git(workspace, "add", "-A")) is None or added.returncode != 0:
        return False
    staged = run_git(workspace, "diff", "--cached", "--quiet")
    if staged is None or staged.returncode == 0:  # nothing staged
        return False
    done = run_git(workspace, "commit", "-q", "--no-verify", "--no-gpg-sign", "-m", message)
    return done is not None and done.returncode == 0


def prepare_workspace(bundle_dir: Path, projects_root: Path) -> Path:
    """Copy a blueprint bundle into a fresh workspace and make it a git repository."""
    bundle_dir = Path(bundle_dir)
    projects_root = Path(projects_root)
    projects_root.mkdir(parents=True, exist_ok=True)
    workspace = projects_root / bundle_dir.name
    if workspace.exists():
        suffix = datetime.now().strftime("%H%M%S%f")
        workspace = projects_root / f"{bundle_dir.name}-{suffix}"
    shutil.copytree(
        bundle_dir, workspace, symlinks=True, ignore=shutil.ignore_patterns(*IGNORED_DIRS, ".DS_Store")
    )
    _write_gitignore(workspace)
    initialised = run_git(workspace, "init", "-q")
    if initialised is not None and initialised.returncode == 0:
        _commit(workspace, "blueprint")
    return workspace


def harden_git_dir(git_dir: Path) -> None:
    """Reset a repository's config, hooks, and attribute overrides to known-good values."""
    (git_dir / "config").write_text(_SAFE_GIT_CONFIG, encoding="utf-8")
    shutil.rmtree(git_dir / "hooks", ignore_errors=True)
    for unsafe in (git_dir / "info" / "attributes", git_dir / "objects" / "info" / "alternates"):
        unsafe.unlink(missing_ok=True)


def commit_workspace(workspace: Path, message: str) -> bool:
    """Commit whatever changed in the workspace (True if a commit was made).

    Refuses a `.git` that is not a plain directory, and resets the repository's config, hooks, and
    attribute overrides first, because agents have been writing in that tree.
    """
    workspace = Path(workspace)
    git_dir = workspace / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        if git_dir.is_symlink():
            LOGGER.warning("%s is a symlink; not committing in that workspace.", git_dir)
        return False
    harden_git_dir(git_dir)
    return _commit(workspace, message)


def head_commit(workspace: Path) -> str | None:
    """Short id of the workspace's current commit (None when there is none or git is unusable)."""
    done = run_git(Path(workspace), "rev-parse", "--short", "HEAD")
    return done.stdout.strip() if done is not None and done.returncode == 0 and done.stdout.strip() else None


def workspace_tree(workspace: Path, limit: int = 60) -> str:
    """Markdown bullet list of the project's files, without dependency, cache, and git noise."""
    workspace = Path(workspace)
    if not workspace.exists():
        return "- (workspace not found)"
    entries: list[str] = []
    for path in sorted(workspace.rglob("*")):
        relative = path.relative_to(workspace)
        if any(part in IGNORED_DIRS for part in relative.parts) or not path.is_file():
            continue
        entries.append(f"- `{relative}`")
        if len(entries) >= limit:
            entries.append("- ... (truncated)")
            break
    return "\n".join(entries) or "- (empty workspace)"


_COMMIT_ID = re.compile(r"^[0-9a-f]{4,40}$")
_DIFF_STAT_LIMIT = 4000


def diff_stat(workspace: Path, commit: str) -> str:
    """`git diff --stat` of what a commit brought in (against its first parent, so a merge shows what it merged).

    `commit` must be a plain hex id: it comes from a file in the workspace, and anything else could be an
    option for git. External diff and text conversion programs are disabled. '' when git is not usable.
    """
    workspace = Path(workspace)
    git_dir = workspace / ".git"
    if not _COMMIT_ID.match(commit or "") or git_dir.is_symlink() or not git_dir.is_dir():
        return ""
    harden_git_dir(git_dir)
    done = run_git(workspace, "diff", "--stat", "--no-ext-diff", "--no-textconv", f"{commit}^1", commit)
    if done is None or done.returncode != 0:  # the first commit has no parent
        done = run_git(workspace, "show", "--stat", "--format=", "--no-ext-diff", "--no-textconv", commit)
    if done is None or done.returncode != 0:
        return ""
    return done.stdout.strip()[:_DIFF_STAT_LIMIT]

"""The artifacts panel: what the user can look at and take away from a run.

The workspace is written by agents, so the read-only viewer treats every path as untrusted: it must stay inside
the workspace (symbolic links that lead out are not followed), never show secret files or git internals, and
never load something large or binary.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from idea_to_mvp.delivery import make_delivery_zip

MAX_VIEW_BYTES = 200_000
_LANGUAGES = {
    ".py": "python",
    ".md": "markdown",
    ".json": "json",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".html": "html",
    ".css": "css",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".sh": "shell",
    ".sql": "sql",
}


@dataclass(frozen=True)
class FileView:
    text: str = ""
    language: str | None = None
    note: str = ""  # why nothing is shown, or a hint


def _hidden(name: str) -> bool:
    return (name == ".env" or name.startswith(".env.")) and name != ".env.example"


def read_workspace_file(workspace: str | Path, selection: Any) -> FileView:
    """The text of a file the user picked in the explorer (a path or a list of paths, relative or absolute)."""
    if isinstance(selection, list | tuple):
        selection = selection[0] if selection else ""
    if not selection:
        return FileView()
    try:
        root = Path(workspace).resolve(strict=True)
        raw = Path(str(selection))
        resolved = (raw if raw.is_absolute() else root / raw).resolve()
    except (OSError, ValueError):
        return FileView(note="That file is not available.")
    if not resolved.is_relative_to(root):
        return FileView(note="That path is outside the workspace.")
    relative = resolved.relative_to(root)
    if ".git" in relative.parts or _hidden(resolved.name):
        return FileView(note="That file is hidden.")
    if not resolved.is_file():
        return FileView()
    try:
        if resolved.stat().st_size > MAX_VIEW_BYTES:
            return FileView(note=f"That file is too large to show ({resolved.stat().st_size // 1000} KB).")
        text = resolved.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return FileView(note="That is a binary file.")
    except OSError:
        return FileView(note="That file is not available.")
    return FileView(text=text, language=_LANGUAGES.get(resolved.suffix.lower()))


def blueprint_zip(bundle_dir: str | Path, dest_dir: Path) -> Path:
    """The blueprint folder as one zip in `dest_dir`."""
    return make_delivery_zip(Path(bundle_dir), dest_dir, label="blueprint")


def artifact_key(workspace_dir: str, bundle_dir: str, delivery_zip: str) -> tuple[str, str, int, str]:
    """Identifies what the panel shows: it changes when the workspace, the delivery zip, or any blueprint file
    (the implement gate lets the user edit them) changes, and only then are the panel's widgets refreshed."""
    newest = 0
    if bundle_dir:
        for root, _dirs, files in os.walk(bundle_dir):
            for name in files:
                try:
                    newest = max(newest, (Path(root) / name).stat().st_mtime_ns)
                except OSError:
                    continue
    return (workspace_dir, bundle_dir, newest, delivery_zip)


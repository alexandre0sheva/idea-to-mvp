"""The *Experience library* panel: browse a stack's lessons and forget an entry (no Gradio imports).

Reads only the library under `OUTPUT_DIR/library/` (`library.py`); forgetting deletes the entry and its harness files
and rebuilds the domain's `INDEX.md`, so the next project of that stack no longer reads it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from idea_to_mvp.library import Entry, list_domains, load_entries, project_name, render_index

EMPTY = (
    "The library is empty. After a delivery the reflection records what the run showed, per stack; the next project "
    "of that stack gives the lessons to its planner as reference data."
)


@dataclass(frozen=True)
class LibraryView:
    """What the panel shows, read once: the stacks, the one picked (None when there is none), and its entries."""

    domains: list[str]
    domain: str | None
    entries: list[Entry]


def read_view(root: Path, domain: str | None) -> LibraryView:
    """The library as the panel shows it; the first stack when `domain` is not one."""
    domains = list_domains(root)
    picked = domain if domain in domains else (domains[0] if domains else None)
    return LibraryView(domains, picked, load_entries(root, picked) if picked else [])


def entry_label(entry: Entry) -> str:
    lesson = entry["lesson"] or {}
    what = f"lesson: {lesson.get('cue', '')[:70]}" if lesson else f"{len(entry['edits'])} edit row(s), no lesson"
    return f"{entry['id']} — {what}"


def entry_choices(entries: Sequence[Entry]) -> list[tuple[str, str]]:
    """(label, entry id) for the entry dropdown, newest first."""
    return [(entry_label(entry), entry["id"]) for entry in reversed(entries)]


def _rows(entry: Entry) -> str:
    rows = "".join(f"\n  - `{row['edit']}`: {row['effect']} — {row['evidence']}" for row in entry["edits"])
    return f"- `{entry['id']}` ({project_name(entry)} v0.{entry['iteration']}){rows}"


def library_markdown(view: LibraryView, *, enabled: bool = True) -> str:
    """The panel's text: the stack's index plus every entry's edit rows (the index lists lessons, not all rows)."""
    note = "" if enabled else "*The library is off (`EXPERIENCE_LIBRARY=off`): runs neither read nor write it.*\n\n"
    if view.domain is None:  # `read_view` picks a stack whenever there is one
        return note + EMPTY
    judged = [entry for entry in view.entries if entry["edits"]]
    rows = "\n\n## Edit rows\n\n" + "\n".join(_rows(entry) for entry in judged) if judged else ""
    return note + render_index(view.domain, view.entries).replace("# Experience library: ", "### ", 1) + rows

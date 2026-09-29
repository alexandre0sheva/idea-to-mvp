"""Regenerate the mermaid graph diagram embedded in docs/architecture.md.

Run after any change to graph topology:  uv run python scripts/gen_graph_diagram.py
`tests/test_docs_sync.py` fails when the committed diagram drifts from the real graph.
"""

from __future__ import annotations

import re
from pathlib import Path

from idea_to_mvp.graph import build_graph

DOC_PATH = Path(__file__).resolve().parents[1] / "docs" / "architecture.md"
START, END = "<!-- graph:start -->", "<!-- graph:end -->"


def render_diagram() -> str:
    mermaid = build_graph().get_graph().draw_mermaid().strip()
    return f"```mermaid\n{mermaid}\n```"


def sync_document(text: str) -> str:
    """Return `text` with the block between the graph markers replaced by the current diagram."""
    pattern = re.compile(re.escape(START) + r".*?" + re.escape(END), flags=re.DOTALL)
    if not pattern.search(text):
        raise ValueError(f"{DOC_PATH} is missing the {START} / {END} markers")
    return pattern.sub(lambda _m: f"{START}\n{render_diagram()}\n{END}", text)


def main() -> None:
    current = DOC_PATH.read_text(encoding="utf-8")
    updated = sync_document(current)
    if updated != current:
        DOC_PATH.write_text(updated, encoding="utf-8")
        print(f"Updated graph diagram in {DOC_PATH}")
    else:
        print("Graph diagram already up to date")


if __name__ == "__main__":
    main()

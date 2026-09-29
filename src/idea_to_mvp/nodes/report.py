from __future__ import annotations

from pathlib import Path
from typing import Any

from idea_to_mvp.state import IdeaDiscussionState

_TREE_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".pytest_cache", ".ruff_cache"}


def workspace_file_tree(workspace: Path, limit: int = 60) -> str:
    if not workspace.exists():
        return "- (workspace not found)"
    entries: list[str] = []
    for path in sorted(workspace.rglob("*")):
        relative = path.relative_to(workspace)
        if any(part in _TREE_SKIP_DIRS for part in relative.parts):
            continue
        if not path.is_file():
            continue
        entries.append(f"- `{relative}`")
        if len(entries) >= limit:
            entries.append("- ... (truncated)")
            break
    return "\n".join(entries) or "- (empty workspace)"


def delivery_report_node(state: IdeaDiscussionState) -> dict[str, Any]:
    workspace_dir = state.get("workspace_dir", "")
    verification = state.get("verification", {})
    verdict = "PASSED ✅" if verification.get("passed") else "FAILED ❌"
    attempts = int(verification.get("attempts") or 0)
    report = (
        "## Delivery report\n\n"
        f"**Workspace:** `{workspace_dir}`\n\n"
        f"**Verification:** {verdict} (after {attempts} fix attempt(s)).\n\n"
        "**How to run:** see `README.md` inside the workspace.\n\n"
        "### Files\n\n"
        f"{workspace_file_tree(Path(workspace_dir)) if workspace_dir else '- (no workspace)'}\n\n"
        "### Verification report\n\n"
        f"{str(verification.get('report') or 'No verification report.').strip()}\n\n"
        "### Implementation summary\n\n"
        f"{(state.get('implementation_log') or 'No implementation summary.').strip()}"
    )
    return {"delivery_report": report, "stage": "done"}

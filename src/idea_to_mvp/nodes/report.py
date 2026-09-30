from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from idea_to_mvp.config import get_settings
from idea_to_mvp.delivery import make_delivery_zip, tag_iteration
from idea_to_mvp.implementation.workspace import workspace_tree
from idea_to_mvp.state import IdeaDiscussionState

LOGGER = logging.getLogger(__name__)


def _bundle(workspace: Path, iteration: int, verified: bool) -> tuple[str, str]:
    """(git tag, zip path) of this delivery. Only a verified version is tagged; the zip is always made so
    the user can take the project along even when verification did not pass. Failures are logged, not fatal."""
    tag = tag_iteration(workspace, iteration) if verified else ""
    try:
        archive = make_delivery_zip(workspace, get_settings().deliveries_dir, label=f"v0.{iteration}")
    except OSError:
        LOGGER.exception("Could not write the delivery zip for %s", workspace)
        return tag, ""
    return tag, str(archive)


def delivery_report_node(state: IdeaDiscussionState) -> dict[str, Any]:
    workspace_dir = state.get("workspace_dir", "")
    verification = state.get("verification", {})
    passed = bool(verification.get("passed"))
    verdict = "PASSED ✅" if passed else "FAILED ❌"
    attempts = int(verification.get("attempts") or 0)
    iteration = int(state.get("iteration") or 1)
    tag, archive = _bundle(Path(workspace_dir), iteration, passed) if workspace_dir and Path(workspace_dir).is_dir() else ("", "")
    lane_verdicts = " · ".join(
        f"{lane.get('lane')} {'✅' if lane.get('passed') else '❌'}" for lane in verification.get("lanes") or []
    )
    lane_line = f"**Lanes:** {lane_verdicts}\n\n" if lane_verdicts else ""
    version = f"**Version:** v0.{iteration}" + (f" (git tag `{tag}`)" if tag else " (not tagged: verification did not pass)") + "\n\n"
    download = f"**Download:** `{archive}`\n\n" if archive else ""
    report = (
        "## Delivery report\n\n"
        f"{version}"
        f"**Workspace:** `{workspace_dir}`\n\n"
        f"{download}"
        f"**Verification:** {verdict} (after {attempts} fix attempt(s)).\n\n"
        f"{lane_line}"
        "**How to run:** see `README.md` inside the workspace.\n\n"
        "### Files\n\n"
        f"{workspace_tree(Path(workspace_dir)) if workspace_dir else '- (no workspace)'}\n\n"
        "### Verification report\n\n"
        f"{str(verification.get('report') or 'No verification report.').strip()}\n\n"
        "### Implementation summary\n\n"
        f"{(state.get('implementation_log') or 'No implementation summary.').strip()}"
    )
    return {"delivery_report": report, "delivery_zip": archive, "stage": "done"}

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from idea_to_mvp import implementer
from idea_to_mvp.config import get_settings
from idea_to_mvp.state import IdeaDiscussionState

LOGGER = logging.getLogger(__name__)


def implementer_node(state: IdeaDiscussionState) -> dict[str, Any]:
    settings = get_settings()
    workspace = implementer.prepare_workspace(Path(state["project_bundle_dir"]), settings.projects_dir)
    LOGGER.info("Implementation workspace prepared at %s", workspace)
    log = implementer.run_implementation(workspace, state.get("execution_strategy") or {}, settings)
    return {"workspace_dir": str(workspace), "implementation_log": log, "stage": "verification"}

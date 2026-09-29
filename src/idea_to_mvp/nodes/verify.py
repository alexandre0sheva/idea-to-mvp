from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from idea_to_mvp import implementer
from idea_to_mvp.config import get_settings
from idea_to_mvp.state import IdeaDiscussionState, VerificationResult

LOGGER = logging.getLogger(__name__)


def verifier_node(state: IdeaDiscussionState) -> dict[str, Any]:
    settings = get_settings()
    workspace = Path(state["workspace_dir"])
    attempts = 0
    result = implementer.run_verification(workspace, settings)
    while not result.get("passed") and attempts < settings.max_fix_attempts:
        attempts += 1
        LOGGER.info("Verification failed; fix attempt %d/%d", attempts, settings.max_fix_attempts)
        implementer.run_fix(workspace, str(result.get("report") or ""), settings)
        result = implementer.run_verification(workspace, settings)
    verification: VerificationResult = {
        "passed": bool(result.get("passed")),
        "attempts": attempts,
        "report": str(result.get("report") or ""),
    }
    return {"verification": verification, "stage": "report"}

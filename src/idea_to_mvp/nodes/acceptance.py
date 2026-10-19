"""The acceptance author: a held-out suite for each version, written before its first task is built.

```
prepare_workspace ─→ acceptance_author ─→ implementer | implement_plan
change_planner    ─┘
```

The role sees the PRD, the plan's public contracts and commands, and (for a later version) the change requests,
and nothing of the implementation. The suite is saved outside the workspace (`implementation/acceptance.py`)
and run by the acceptance lane of every verification round of that version.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from idea_to_mvp import llm
from idea_to_mvp.config import get_settings
from idea_to_mvp.implementation.acceptance import SUITE_FILE, AcceptanceSuite, save_suite, suite_dir
from idea_to_mvp.implementation.verify import ACCEPTANCE, LANES, LaneName, priority_requirements
from idea_to_mvp.plan import Plan, load_plan
from idea_to_mvp.state import IdeaDiscussionState
from idea_to_mvp.text_utils import read_text
from idea_to_mvp.usage import with_usage

LOGGER = logging.getLogger(__name__)


def _record(iteration: int, *, folder: Path | None = None, files: list[str] | None = None, covers: list[str] | None = None, note: str = "") -> dict[str, Any]:
    """The state's `acceptance` record: one shape whether a suite was written or not."""
    return {"iteration": iteration, "dir": str(folder) if folder else "", "files": files or [], "covers": covers or [], "note": note}


def _no_suite() -> AcceptanceSuite:
    return AcceptanceSuite(files=[], command="", covers=[])


def author_prompt(prd: str, plan: Plan, change_requests: list[str]) -> str:
    """What the author may know: the PRD, the public contracts, the commands, and the changes asked for since."""
    required = priority_requirements(prd)
    contracts = "\n".join(f"- {c.id} {c.name}: {c.description}" for c in plan.contracts) or "- (none)"
    commands = plan.commands
    command_lines = "\n".join(
        f"- {name}: `{value}`"
        for name, value in (("install", commands.install), ("test", commands.test), ("run", commands.run))
        if value
    )
    listed_required = ", ".join(f"{rid} ({priority or 'unprioritised'})" for rid, priority in required.items())
    changes = ""
    if change_requests:
        listed = "\n".join(f"{n}. {request.strip()}" for n, request in enumerate(change_requests, start=2))
        changes = (
            "\n\n## Changes since the PRD (one per later version; a change wins where it conflicts with the PRD)\n"
            f"{listed}\n"
        )
    return (
        f"## PRD.md\n{prd.strip() or '(empty)'}\n\n"
        f"## Requirements every suite must cover\n{listed_required or '(none could be parsed)'}\n\n"
        f"## Public contracts (plan.json)\n{contracts}\n\n"
        f"## Project commands\n{command_lines or '- (none)'}"
        f"{changes}\n\n"
        "Write the acceptance suite now."
    )


@with_usage(role="acceptance_author")
def acceptance_author_node(state: IdeaDiscussionState) -> dict[str, Any]:
    settings = get_settings()
    iteration = int(state.get("iteration") or 1)
    if not settings.acceptance_enabled:
        return {"acceptance": {}}
    workspace = Path(state["workspace_dir"])
    plan = load_plan(read_text(workspace / "plan.json"))
    if plan is None:
        LOGGER.warning("No plan.json in %s: no acceptance tests for v0.%d.", workspace, iteration)
        return {"acceptance": _record(iteration, note="No plan to write acceptance tests from.")}
    runtime = llm.get_runtime("acceptance_author")
    suite = llm.invoke_structured(
        runtime,
        [
            SystemMessage(content=runtime.system_prompt),
            HumanMessage(content=author_prompt(read_text(workspace / "PRD.md"), plan, list(state.get("change_requests") or []))),
        ],
        AcceptanceSuite,
        fallback=_no_suite,
    )
    if not suite.files:
        LOGGER.warning("The acceptance author wrote no tests for v0.%d; verification runs without the acceptance lane.", iteration)
        return {"acceptance": _record(iteration, note="The acceptance author wrote no tests.")}
    folder = suite_dir(settings, workspace, iteration)
    files = save_suite(folder, suite)
    LOGGER.info("Wrote %d held-out acceptance file(s) for v0.%d to %s", len(files), iteration, folder)
    return {"acceptance": _record(iteration, folder=folder, files=files, covers=suite.covers)}


def lanes_for(state: IdeaDiscussionState) -> tuple[LaneName, ...]:
    """The lanes of a verification round: the three every round has, plus acceptance when this version has a suite
    that is still on disk (one deleted since, or moved with OUTPUT_DIR, would only fail every round, and no fix
    session could repair that)."""
    suite = state.get("acceptance") or {}
    current = int(suite.get("iteration") or 0) == int(state.get("iteration") or 1)
    folder = str(suite.get("dir") or "")
    if not (current and suite.get("files") and folder):
        return LANES
    if not (Path(folder) / SUITE_FILE).is_file():
        LOGGER.warning("The acceptance suite of v0.%s is gone from %s; verifying without the acceptance lane.", suite.get("iteration"), folder)
        return LANES
    return (*LANES, ACCEPTANCE)

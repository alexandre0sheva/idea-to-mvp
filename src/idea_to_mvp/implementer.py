"""Implementation stage: drives Claude Agent SDK sessions that build the MVP.

The blueprint bundle is copied into ``<projects_dir>/<bundle-name>/`` (a git repository, see
``implementation/workspace.py``) and one or more agent sessions implement it there, confined by
``implementation/options.py`` (OS sandbox, tool-call guard, scrubbed environment; see SECURITY.md).
Two execution modes (chosen by the strategy agent):

- ``subagents``: a single lead session that delegates to per-workstream subagents
  (passed programmatically as ``AgentDefinition`` objects, with prompts taken from
  the blueprint's ``.claude/agents/*.md`` files when present).
- ``agent_team``: one fresh session per plan task, scheduled by ``implementation/executor.py``
  (in parallel git worktrees where the plan allows, see ``nodes/implement_graph.py``). This module's
  ``run_implementation`` only runs ``agent_team`` as one session per *workstream*, one after the
  other, for a project whose ``plan.json`` cannot be scheduled; later sessions see earlier work
  through the shared filesystem.

Every session is recorded as a trajectory and a ledger row (``implementation/trajectory.py``).

All SDK imports are local to the functions that need them so the rest of the app
imports without the SDK and tests can monkeypatch these functions wholesale.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from idea_to_mvp.implementation.options import SandboxUnavailableError, build_agent_options
from idea_to_mvp.implementation.trajectory import SessionLog, model_label
from idea_to_mvp.implementation.workspace import commit_workspace, prepare_workspace

if TYPE_CHECKING:  # pragma: no cover - typing only
    from idea_to_mvp.config import Settings
    from idea_to_mvp.model_catalog import AgentRole

LOGGER = logging.getLogger(__name__)

__all__ = [
    "commit_workspace",
    "iter_agent_messages",
    "prepare_workspace",
    "run_implementation",
]

LEAD_PROMPT = (
    "You are the lead implementation agent for this brand-new MVP project.\n"
    "Read `AGENTS.md`, `PRD.md`, `ARCHITECTURE.md`, and `plan.md` first; `STRATEGY.json` lists your "
    "specialized subagents (also defined in `.claude/agents/`).\n"
    "Implement the full plan in `plan.md`, task by task and in order, delegating workstream-specific tasks "
    "to the matching subagent. Every task must land with its required tests; keep the project's test suite "
    "green at all times. When the plan is complete, make sure README.md documents how to install, run, and "
    "test the project, then summarize what was built and how to run it."
)

TEAM_SESSION_PROMPT = (
    "You are the `{name}` implementation agent on a multi-agent team building this MVP.\n"
    "Workstream focus: {focus}\n"
    "Deliverables: {deliverables}\n"
    "Read `AGENTS.md`, `PRD.md`, `ARCHITECTURE.md`, and `plan.md` first. Implement only the `plan.md` tasks "
    "assigned to the `{name}` workstream, in order, respecting the contract registry. Earlier teammates may "
    "already have produced code - build on it, never rewrite their workstreams. Every task lands with its "
    "required tests and the full test suite must stay green. Finish with a short summary of what you built."
)


def _subagent_prompt(workspace: Path | None, name: str, fallback: str) -> str:
    if workspace is not None:
        path = Path(workspace) / ".claude" / "agents" / f"{name}.md"
        if path.exists():
            text = path.read_text(encoding="utf-8")
            body = re.sub(r"^---\n.*?\n---\n", "", text, flags=re.DOTALL).strip()
            if body:
                return body
    return fallback


def _build_agent_definitions(strategy: dict[str, Any], workspace: Path | None) -> dict[str, Any]:
    from claude_agent_sdk import AgentDefinition

    definitions: dict[str, Any] = {}
    for workstream in (strategy or {}).get("workstreams") or []:
        name = str(workstream.get("name") or "").strip()
        if not name:
            continue
        focus = str(workstream.get("focus") or "").strip()
        deliverables = str(workstream.get("deliverables") or "").strip()
        fallback_prompt = (
            f"You are the `{name}` implementation agent. Focus: {focus or 'general implementation'}. "
            f"Deliverables: {deliverables or 'working, tested code'}. Read `AGENTS.md`, `PRD.md`, "
            "`ARCHITECTURE.md`, and `plan.md`, implement your assigned tasks with tests, and keep the "
            "project test suite green."
        )
        definitions[name] = AgentDefinition(
            description=f"Implements the `{name}` workstream. {focus}".strip(),
            prompt=_subagent_prompt(workspace, name, fallback_prompt),
            model="inherit",
        )
    return definitions


async def iter_agent_messages(
    prompt: str,
    *,
    workspace: Path,
    settings: Settings,
    agents: dict[str, Any] | None = None,
    max_turns: int,
    max_budget_usd: float | None = None,
    output_schema: dict[str, Any] | None = None,
    extra_disallowed_tools: Sequence[str] = (),
    role: AgentRole = "implementer",
    query_fn: Callable[..., AsyncIterator[Any]] | None = None,
) -> AsyncIterator[Any]:
    """Stream the SDK messages of one confined agent session of kind `role` (`query_fn` lets tests inject a fake)."""
    from claude_agent_sdk import query as sdk_query

    run_query = query_fn or sdk_query
    options = build_agent_options(
        workspace=workspace,
        settings=settings,
        max_turns=max_turns,
        agents=agents,
        max_budget_usd=max_budget_usd,
        output_schema=output_schema,
        extra_disallowed_tools=extra_disallowed_tools,
        role=role,
    )
    async for message in run_query(prompt=prompt, options=options):
        yield message


async def _run_agent_async(
    prompt: str,
    *,
    workspace: Path,
    settings: Settings,
    agents: dict[str, Any] | None = None,
    max_turns: int,
    max_budget_usd: float | None = None,
    log: SessionLog | None = None,
) -> str:
    from claude_agent_sdk import ResultMessage

    result_text = ""
    cost: float | None = None
    turns, session_id, failed = 0, None, False
    try:
        async for message in iter_agent_messages(
            prompt,
            workspace=workspace,
            settings=settings,
            agents=agents,
            max_turns=max_turns,
            max_budget_usd=max_budget_usd,
        ):
            if log is not None:
                log.message(message)
            if isinstance(message, ResultMessage):
                result_text = (message.result or "").strip()
                if message.is_error:
                    result_text = (
                        f"Agent run ended with an error ({message.subtype}). {result_text}".strip()
                    )
                    failed = True
                cost, turns, session_id = (
                    message.total_cost_usd,
                    message.num_turns,
                    message.session_id,
                )
                LOGGER.info(
                    "Agent session finished: turns=%s cost_usd=%s error=%s",
                    message.num_turns,
                    f"{cost:.4f}" if isinstance(cost, (int, float)) else "n/a",
                    message.is_error,
                )
    except SandboxUnavailableError:
        raise  # a configuration problem: no session ran
    except Exception as exc:
        if log is not None:
            log.finish(
                outcome="crashed", cost_usd=float(cost or 0.0), turns=turns, session_id=session_id,
                note=f"{type(exc).__name__}: {exc}",
            )  # fmt: skip
        raise
    if log is not None:
        log.finish(
            outcome="failed" if failed else "success", cost_usd=float(cost or 0.0), turns=turns, session_id=session_id,
            note=result_text if failed else "",
        )  # fmt: skip
    return result_text


def _run_agent(
    prompt: str,
    *,
    workspace: Path,
    settings: Settings,
    agents: dict[str, Any] | None = None,
    max_turns: int,
    max_budget_usd: float | None = None,
    label: str,
) -> str:
    """Run one session to its end on its own event loop, recording it under `label` (this runs in a worker thread)."""
    log = SessionLog(
        workspace, kind="task", label=label, model=model_label(settings, "implementer")
    )
    return asyncio.run(
        _run_agent_async(
            prompt,
            workspace=workspace,
            settings=settings,
            agents=agents,
            max_turns=max_turns,
            max_budget_usd=max_budget_usd,
            log=log,
        )
    )


def run_implementation(workspace: Path, strategy: dict[str, Any], settings: Settings) -> str:
    """Build the project in `workspace` according to the execution strategy."""
    if settings.demo_mode:
        from idea_to_mvp.demo.implementer import demo_implement

        return demo_implement(Path(workspace), strategy)
    workspace = Path(workspace)
    mode = (strategy or {}).get("mode") or "subagents"
    workstreams = (strategy or {}).get("workstreams") or []

    if mode == "agent_team" and workstreams:
        logs: list[str] = []
        for workstream in workstreams:
            name = str(workstream.get("name") or "workstream").strip() or "workstream"
            prompt = TEAM_SESSION_PROMPT.format(
                name=name,
                focus=str(workstream.get("focus") or "general implementation"),
                deliverables=str(workstream.get("deliverables") or "working, tested code"),
            )
            LOGGER.info("Starting agent-team session for workstream %s", name)
            session_log = _run_agent(
                prompt,
                workspace=workspace,
                settings=settings,
                max_turns=settings.implementer_max_turns,
                max_budget_usd=settings.implementer_max_total_usd
                / len(workstreams),  # an equal share each
                label=f"workstream-{name}",
            )
            logs.append(f"## Workstream `{name}`\n\n{session_log or 'No summary produced.'}")
        return "\n\n".join(logs)

    agent_definitions = _build_agent_definitions(strategy or {}, workspace)
    LOGGER.info("Starting lead implementation session with %d subagents", len(agent_definitions))
    log = _run_agent(
        LEAD_PROMPT,
        workspace=workspace,
        settings=settings,
        agents=agent_definitions or None,
        max_turns=settings.implementer_max_turns,
        max_budget_usd=settings.implementer_max_total_usd,  # one session builds everything
        label="lead",
    )
    return log or "No implementation summary produced."

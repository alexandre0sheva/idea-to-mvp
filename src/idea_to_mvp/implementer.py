"""Implementation stage: drives Claude Agent SDK sessions that build the MVP.

The blueprint bundle is copied into ``<projects_dir>/<bundle-name>/`` and one
or more agent sessions implement it there, sandboxed to that working directory.
Two execution modes (chosen by the strategy agent):

- ``subagents``: a single lead session that delegates to per-workstream subagents
  (passed programmatically as ``AgentDefinition`` objects, with prompts taken from
  the blueprint's ``.claude/agents/*.md`` files when present).
- ``agent_team``: one focused session per workstream, run sequentially; later
  sessions see earlier work through the shared filesystem.

All SDK imports are local to the functions that need them so the rest of the app
imports without the SDK and tests can monkeypatch these functions wholesale.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from idea_to_mvp.config import Settings
LOGGER = logging.getLogger(__name__)

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

VERIFICATION_PROMPT = (
    "You are the verification agent for this project.\n"
    "1. Read `README.md` and `quality/AGENTS.md` to find the install and test commands.\n"
    "2. Install dependencies if needed, then run the full test suite (plus lint, if configured).\n"
    "3. Report: how many tests ran, what failed, and the relevant error output.\n"
    "Do NOT fix anything - only verify and report.\n"
    "End your final message with exactly one line: `VERDICT: PASS` if everything succeeds, "
    "otherwise `VERDICT: FAIL`."
)

FIX_PROMPT = (
    "You are the implementation agent for this project. A verification run just failed.\n"
    "Verification report:\n\n{report}\n\n"
    "Diagnose and fix the failures, keeping changes minimal and consistent with `ARCHITECTURE.md` and the "
    "contracts in `plan.md`. Re-run the affected tests to confirm the fix, then summarize what you changed."
)


def prepare_workspace(bundle_dir: Path, projects_root: Path) -> Path:
    """Copy a blueprint bundle into a fresh implementation workspace."""
    bundle_dir = Path(bundle_dir)
    projects_root = Path(projects_root)
    projects_root.mkdir(parents=True, exist_ok=True)
    workspace = projects_root / bundle_dir.name
    if workspace.exists():
        suffix = datetime.now().strftime("%H%M%S%f")
        workspace = projects_root / f"{bundle_dir.name}-{suffix}"
    shutil.copytree(bundle_dir, workspace)
    return workspace


def _parse_verdict(text: str) -> bool | None:
    matches = re.findall(r"VERDICT:\s*(PASS|FAIL)", text or "", flags=re.IGNORECASE)
    if not matches:
        return None
    return matches[-1].upper() == "PASS"


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


async def _run_agent_async(
    prompt: str,
    *,
    workspace: Path,
    settings: Settings,
    agents: dict[str, Any] | None = None,
    max_turns: int,
) -> str:
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

    options = ClaudeAgentOptions(
        cwd=str(workspace),
        model=settings.implementer_model,
        permission_mode=settings.implementer_permission_mode,
        max_turns=max_turns,
        max_budget_usd=settings.implementer_max_budget_usd,
        agents=agents or None,
    )
    result_text = ""
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            result_text = (message.result or "").strip()
            if message.is_error:
                result_text = f"Agent run ended with an error ({message.subtype}). {result_text}".strip()
            cost = message.total_cost_usd
            LOGGER.info(
                "Agent session finished: turns=%s cost_usd=%s error=%s",
                message.num_turns,
                f"{cost:.4f}" if isinstance(cost, (int, float)) else "n/a",
                message.is_error,
            )
    return result_text


def _run_agent(
    prompt: str,
    *,
    workspace: Path,
    settings: Settings,
    agents: dict[str, Any] | None = None,
    max_turns: int,
) -> str:
    return asyncio.run(
        _run_agent_async(prompt, workspace=workspace, settings=settings, agents=agents, max_turns=max_turns)
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
                prompt, workspace=workspace, settings=settings, max_turns=settings.implementer_max_turns
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
    )
    return log or "No implementation summary produced."


def run_verification(workspace: Path, settings: Settings) -> dict[str, Any]:
    """Run the generated project's own test suite via a verification agent."""
    if settings.demo_mode:
        from idea_to_mvp.demo.implementer import demo_verify

        return demo_verify(Path(workspace))
    report = _run_agent(
        VERIFICATION_PROMPT,
        workspace=Path(workspace),
        settings=settings,
        max_turns=settings.verifier_max_turns,
    )
    verdict = _parse_verdict(report)
    if verdict is None:
        LOGGER.warning("Verifier did not produce an explicit verdict; treating as FAIL.")
    return {
        "passed": bool(verdict),
        "report": report or "Verifier produced no report.",
    }


def run_fix(workspace: Path, report: str, settings: Settings) -> str:
    """Feed a failed verification report back to an implementation agent."""
    if settings.demo_mode:
        from idea_to_mvp.demo.implementer import demo_fix

        return demo_fix(report)
    return _run_agent(
        FIX_PROMPT.format(report=report or "No report available."),
        workspace=Path(workspace),
        settings=settings,
        max_turns=settings.implementer_max_turns,
    )

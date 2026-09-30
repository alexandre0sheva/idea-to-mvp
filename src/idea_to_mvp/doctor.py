"""`idea-to-mvp doctor`: verify keys, models, and the agent CLI before a run spends time or money."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import HumanMessage

from idea_to_mvp import llm
from idea_to_mvp.config import Settings
from idea_to_mvp.roles import ROLES, resolve_role_model

Status = Literal["ok", "warn", "fail"]

_KEYS: dict[str, tuple[str, str]] = {
    "openai": ("openai_api_key", "OPENAI_API_KEY"),
    "anthropic": ("anthropic_api_key", "ANTHROPIC_API_KEY"),
    "google": ("google_api_key", "GOOGLE_API_KEY"),
}
_PING_PROMPT = "Reply with the single word OK."


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str


def default_ping(runtime: Any) -> str:
    """Cheapest real round-trip to the model behind a role."""
    return llm.invoke_text(runtime, [HumanMessage(content=_PING_PROMPT)], max_tokens=16)


def _cli_path() -> str | None:
    try:
        import claude_agent_sdk

        for name in ("claude", "claude.exe"):
            bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / name
            if bundled.exists():
                return str(bundled)
    except ImportError:
        return None
    return shutil.which("claude")


def _implementer_checks(settings: Settings) -> list[Check]:
    checks: list[Check] = []
    label = f"implementer ({settings.implementer_model})"
    cli = _cli_path()
    checks.append(
        Check(f"{label} CLI", "ok", f"Claude Code CLI found at {cli}")
        if cli
        else Check(f"{label} CLI", "fail", "Claude Code CLI not found; reinstall claude-agent-sdk")
    )
    checks.append(
        Check(f"{label} credentials", "ok", "ANTHROPIC_API_KEY is set")
        if settings.anthropic_api_key
        else Check(
            f"{label} credentials",
            "warn",
            "ANTHROPIC_API_KEY not set; the implementation stage will need a logged-in Claude Code session",
        )
    )
    return checks


def collect_checks(
    settings: Settings,
    *,
    runtime_for: Callable[[str], Any] = llm.get_runtime,
    ping: Callable[[Any], str] = default_ping,
    offline: bool = False,
) -> list[Check]:
    """Run every check; failures are reported, never raised."""
    if settings.demo_mode:
        return [Check("demo mode", "ok", "DEMO_MODE is on: canned outputs, no keys or model calls needed")]

    checks: list[Check] = []
    pinged: dict[tuple[str, str], tuple[Status, str]] = {}
    for role in ROLES:
        if role == "researcher" and not settings.enable_research:
            continue  # an unused role's key and model are not this run's problem
        provider, model = resolve_role_model(settings, role)
        name = f"{role} ({provider}:{model})"
        field, env_name = _KEYS[provider]
        if not getattr(settings, field):
            checks.append(Check(name, "fail", f"{env_name} is not set"))
            continue
        if offline:
            checks.append(Check(name, "ok", "key is set (not pinged: --offline)"))
            continue
        key = (provider, model)
        if key not in pinged:
            try:
                reply = ping(runtime_for(role))
                pinged[key] = ("ok", "reachable") if reply.strip() else ("fail", "model returned no text")
            except Exception as exc:
                pinged[key] = ("fail", f"{type(exc).__name__}: {exc}"[:240])
        status, detail = pinged[key]
        checks.append(Check(name, status, detail))
    checks.extend(_implementer_checks(settings))
    return checks


def format_report(checks: list[Check]) -> str:
    width = max((len(c.name) for c in checks), default=0)
    lines = [f"{c.status.upper():<4}  {c.name:<{width}}  {c.detail}" for c in checks]
    return "\n".join(lines)


def run_doctor(
    settings: Settings,
    *,
    runtime_for: Callable[[str], Any] = llm.get_runtime,
    ping: Callable[[Any], str] = default_ping,
    offline: bool = False,
    out: Callable[[str], None] = print,
) -> int:
    """Print the report; exit code 1 if any check failed."""
    checks = collect_checks(settings, runtime_for=runtime_for, ping=ping, offline=offline)
    out(format_report(checks))
    failed = [c for c in checks if c.status == "fail"]
    out(f"\n{len(failed)} problem(s) found." if failed else "\nAll checks passed.")
    return 1 if failed else 0

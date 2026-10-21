"""`idea-to-mvp doctor`: verify keys, models, and the agent CLI before a run spends time or money."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import HumanMessage

from idea_to_mvp import llm
from idea_to_mvp.config import Settings
from idea_to_mvp.model_catalog import AGENT_ROLES, ModelCatalog, get_catalog
from idea_to_mvp.pricing import PricingError, settings_overrides
from idea_to_mvp.roles import ROLES, resolve_agent_model, resolve_role_model
from idea_to_mvp.templates import load_templates, missing_tools

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
    label = f"implementer ({resolve_agent_model(settings, 'implementer')})"
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


def _catalog_checks(settings: Settings, catalog: ModelCatalog) -> list[Check]:
    """Warn about a configured model the catalog does not know, has no price for, or marks as retiring."""
    used: dict[str, None] = {}  # insertion-ordered set
    for role in ROLES:
        if role == "researcher" and not settings.enable_research:
            continue
        used[resolve_role_model(settings, role)[1]] = None
    for kind in AGENT_ROLES:
        used[resolve_agent_model(settings, kind)] = None
    checks: list[Check] = []
    for model in used:
        item = catalog.find(model)
        name = f"catalog {model}"
        if item is None:
            checks.append(
                Check(
                    name,
                    "warn",
                    "not in the model catalog (models.toml): its price and capabilities are unknown",
                )
            )
        elif not item.priced:
            checks.append(
                Check(name, "warn", "no price in the model catalog: cost figures leave it out")
            )
        elif item.status == "deprecated" or item.retired(date.today()):
            checks.append(
                Check(name, "warn", "deprecated or retired according to the model catalog")
            )
    return checks


def _template_checks(which: Callable[[str], str | None]) -> list[Check]:
    """Warn when the toolchain of a starter template is missing: the strategy may pick the template, and the
    agent sessions then install and run its scaffold with these tools. Templates that need the same tools
    are reported together."""
    by_toolchain: dict[tuple[str, ...], list[str]] = {}
    for template in load_templates().values():
        by_toolchain.setdefault(template.toolchain, []).append(template.name)
    checks: list[Check] = []
    for toolchain, names in by_toolchain.items():
        label = f"template {', '.join(names)}"
        missing = [
            tool
            for template in load_templates().values()
            if template.toolchain == toolchain
            for tool in missing_tools(template, which)
        ]
        if missing:
            checks.append(
                Check(
                    label,
                    "warn",
                    f"{', '.join(dict.fromkeys(missing))} not found on the PATH: a project from this template cannot be installed or tested here",
                )
            )
        else:
            checks.append(Check(label, "ok", f"{', '.join(toolchain)} found"))
    return checks


def _prices_checks(settings: Settings) -> list[Check]:
    """Warn when MODEL_PRICES_FILE is set but cannot be used (cost figures then fall back to the catalog)."""
    if not settings.model_prices_file:
        return []
    try:
        prices = settings_overrides(settings)
    except PricingError as exc:
        return [Check("model prices file", "warn", f"{exc}; catalog prices are used instead")]
    return [
        Check(
            "model prices file",
            "ok",
            f"{len(prices)} model price(s) from {settings.model_prices_file}",
        )
    ]


def collect_checks(
    settings: Settings,
    *,
    runtime_for: Callable[[str], Any] = llm.get_runtime,
    ping: Callable[[Any], str] = default_ping,
    offline: bool = False,
    catalog: ModelCatalog | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> list[Check]:
    """Run every check; failures are reported, never raised."""
    if settings.demo_mode:
        return [
            Check(
                "demo mode", "ok", "DEMO_MODE is on: canned outputs, no keys or model calls needed"
            )
        ]

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
                pinged[key] = (
                    ("ok", "reachable") if reply.strip() else ("fail", "model returned no text")
                )
            except Exception as exc:
                pinged[key] = ("fail", f"{type(exc).__name__}: {exc}"[:240])
        status, detail = pinged[key]
        checks.append(Check(name, status, detail))
    checks.extend(_implementer_checks(settings))
    checks.extend(_catalog_checks(settings, catalog or get_catalog()))
    checks.extend(_template_checks(which))
    checks.extend(_prices_checks(settings))
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

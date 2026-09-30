"""The single place that decides how an implementation agent is confined.

Four layers, none of which is enough alone (SECURITY.md has the threat model):

1. **OS sandbox** for shell commands (`SandboxSettings`): writes only inside the workspace, network only to
   the allowed package registries, no credential directories, no escape via `dangerouslyDisableSandbox`.
2. **Tool-call guard** (`guard.py`, a `PreToolUse` hook): confines file tools to the workspace and refuses
   dangerous shell commands. File tools do not run inside the OS sandbox, so this is their only check.
3. **Environment scrub**: provider keys and other secrets in the orchestrator's environment are blanked for
   the agent process. The SDK merges `ClaudeAgentOptions.env` *over* the inherited environment, so a secret
   can only be removed by overriding it with an empty string.
4. **Settings isolation**: `setting_sources=[]`. By default the CLI would load the user's
   `~/.claude/settings.json` and the workspace's `.claude/settings.json`, which agents can write.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from idea_to_mvp.implementation.guard import GUARDED_TOOLS, bind_workspace

if TYPE_CHECKING:  # pragma: no cover - typing only
    from claude_agent_sdk import ClaudeAgentOptions
    from claude_agent_sdk.types import SandboxSettings

    from idea_to_mvp.config import Settings

LOGGER = logging.getLogger(__name__)

# Names that look like secrets. Variables for the agent's own Anthropic access are kept.
_SECRET_NAME = re.compile(
    r"(_API_KEY|_TOKEN|_SECRET|_SECRET_KEY|_ACCESS_KEY|_PASSWORD|_PASSWD|_CREDENTIALS?)$", re.IGNORECASE
)
_KEEP_PREFIXES = ("ANTHROPIC_", "CLAUDE_")
# Credential locations the sandbox must not let shell commands read (relative to the user's home).
_CREDENTIAL_PATHS = (
    ".ssh",
    ".aws",
    ".gnupg",
    ".config/gh",
    ".config/gcloud",
    ".azure",
    ".kube",
    ".docker",
    ".netrc",
    ".git-credentials",
    ".npmrc",
    ".pypirc",
)
# Web tools are not covered by the sandbox's network allowlist, and a URL can carry data out.
_DISALLOWED_TOOLS = ["WebFetch", "WebSearch"]


class SandboxUnavailableError(RuntimeError):
    """IMPLEMENTER_SANDBOX=on but this machine cannot provide the OS sandbox."""


@dataclass(frozen=True)
class SandboxStatus:
    enabled: bool
    summary: str  # one sentence for the implement gate


def sandbox_supported(
    platform: str | None = None, which: Callable[[str], str | None] = shutil.which
) -> bool:
    """Whether the OS sandbox can run here: Seatbelt on macOS; bubblewrap and socat on Linux."""
    platform = platform or sys.platform
    if platform == "darwin":
        return which("sandbox-exec") is not None
    if platform.startswith("linux"):
        return which("bwrap") is not None and which("socat") is not None
    return False


def _mechanism() -> str:
    return "macOS Seatbelt" if sys.platform == "darwin" else "bubblewrap"


def _resolve_sandbox(settings: Settings) -> tuple[bool, str]:
    """(enabled, why not): `on` refuses to run without the sandbox, `auto` falls back with a warning."""
    if settings.implementer_sandbox == "off":
        return False, "IMPLEMENTER_SANDBOX=off"
    if sandbox_supported():
        return True, ""
    if settings.implementer_sandbox == "on":
        raise SandboxUnavailableError(
            "IMPLEMENTER_SANDBOX=on, but the OS sandbox is not supported here (macOS needs sandbox-exec; "
            "Linux needs bubblewrap and socat). Install them, set IMPLEMENTER_SANDBOX=auto, or run inside a container."
        )
    return False, "the OS sandbox is not supported on this platform"


def sandbox_status(settings: Settings) -> SandboxStatus:
    """What to tell the user at the implement gate; never claims more than is true."""
    if settings.demo_mode:
        return SandboxStatus(False, "demo mode (no real agents run, so nothing is executed or sandboxed)")
    try:
        enabled, reason = _resolve_sandbox(settings)
    except SandboxUnavailableError:
        return SandboxStatus(False, "required (IMPLEMENTER_SANDBOX=on) but not supported here; the run will be refused")
    if enabled:
        domains = ", ".join(settings.implementer_allowed_domains) or "no network hosts"
        return SandboxStatus(
            True,
            f"on ({_mechanism()}): shell commands can write only inside the workspace, cannot read your "
            f"credentials, and can reach only {domains}",
        )
    if settings.implementer_sandbox == "off":
        return SandboxStatus(False, "OFF (IMPLEMENTER_SANDBOX=off): shell commands run with your full user permissions")
    return SandboxStatus(
        False,
        "not supported on this platform, so shell commands run with your full user permissions "
        "and only the tool-call guard applies",
    )


def scrubbed_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Overrides that blank every secret-looking variable (except the agent's own Anthropic access).

    Empty strings, not removal: the SDK merges these over the inherited environment.
    """
    environ = os.environ if environ is None else environ
    return {
        name: ""
        for name in environ
        if _SECRET_NAME.search(name) and not name.upper().startswith(_KEEP_PREFIXES)
    }


def _sandbox_settings(settings: Settings) -> SandboxSettings:
    home = Path.home()
    deny_read = [str(home / relative) for relative in _CREDENTIAL_PATHS]
    deny_read.append(str(Path.cwd() / ".env"))  # the orchestrator's own secrets
    sandbox: dict[str, Any] = {
        "enabled": True,
        "autoAllowBashIfSandboxed": True,
        "allowUnsandboxedCommands": False,
        "network": {"allowedDomains": list(settings.implementer_allowed_domains)},
        "filesystem": {"denyRead": deny_read},  # writes are already limited to the working directory
    }
    return cast("SandboxSettings", sandbox)


def build_agent_options(
    *,
    workspace: Path,
    settings: Settings,
    max_turns: int,
    agents: dict[str, Any] | None = None,
    max_budget_usd: float | None = None,
    output_schema: dict[str, Any] | None = None,
    extra_disallowed_tools: Sequence[str] = (),
) -> ClaudeAgentOptions:
    """Options for one agent session in `workspace`: cwd, model, budget, sandbox, guard hook, env, tools.

    `output_schema` makes the session end with a JSON result of that shape (`ResultMessage.structured_output`);
    `extra_disallowed_tools` adds to the tools no session may use (read-mostly verification lanes).
    """
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

    enabled, reason = _resolve_sandbox(settings)
    if not enabled:
        LOGGER.warning(
            "Implementation sandbox is OFF (%s): agent shell commands run with your full user permissions; "
            "only the tool-call guard applies. For untrusted ideas run idea-to-mvp inside a container or VM.",
            reason,
        )
        if settings.implementer_permission_mode != "bypassPermissions":
            LOGGER.warning(
                "Without the sandbox, shell commands need approval that a headless run cannot give, so the "
                "agents will be unable to run tests. Set IMPLEMENTER_PERMISSION_MODE=bypassPermissions to accept "
                "the risk, or enable the sandbox."
            )
    return ClaudeAgentOptions(
        cwd=str(workspace),
        model=settings.implementer_model,
        permission_mode=settings.implementer_permission_mode,
        max_turns=max_turns,
        max_budget_usd=settings.implementer_max_task_usd if max_budget_usd is None else max_budget_usd,
        agents=agents or None,
        setting_sources=[],
        disallowed_tools=[*_DISALLOWED_TOOLS, *extra_disallowed_tools],
        output_format={"type": "json_schema", "schema": output_schema} if output_schema else None,
        env=scrubbed_env(),
        sandbox=_sandbox_settings(settings) if enabled else None,
        hooks={"PreToolUse": [HookMatcher(matcher=GUARDED_TOOLS, hooks=[bind_workspace(Path(workspace))])]},
    )

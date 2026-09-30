"""build_agent_options: the one place that decides how an implementation agent is confined."""

import logging
from pathlib import Path

import pytest

from idea_to_mvp.config import Settings
from idea_to_mvp.implementation import options as opts
from idea_to_mvp.implementation.options import (
    SandboxUnavailableError,
    build_agent_options,
    sandbox_status,
    sandbox_supported,
    scrubbed_env,
)

SECRETS = {
    "OPENAI_API_KEY": "sk-openai",
    "GOOGLE_API_KEY": "g-key",
    "GEMINI_API_KEY": "gem",
    "LANGSMITH_API_KEY": "ls",
    "GITHUB_TOKEN": "ghp_x",
    "GH_TOKEN": "ghp_y",
    "NPM_TOKEN": "npm",
    "MY_SERVICE_TOKEN": "t",
    "AWS_SECRET_ACCESS_KEY": "aws",
    "DB_PASSWORD": "pw",
    "STRIPE_SECRET": "s",
}
KEEP = {
    "ANTHROPIC_API_KEY": "sk-ant",
    "ANTHROPIC_BASE_URL": "https://proxy",
    "CLAUDE_CODE_OAUTH_TOKEN": "oauth",
    "PATH": "/usr/bin",
    "HOME": "/home/x",
    "LANG": "en_US.UTF-8",
}


def settings_with(**overrides) -> Settings:
    return Settings(_env_file=None, implementer_sandbox="on", **overrides)


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "projects" / "demo"
    path.mkdir(parents=True)
    return path


@pytest.fixture(autouse=True)
def sandbox_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(opts, "sandbox_supported", lambda *a, **k: True)


# ----------------------------------------------------------------- env scrub


def test_secrets_are_blanked_with_empty_strings_because_the_sdk_merges_env_over_the_inherited_one() -> None:
    env = scrubbed_env({**SECRETS, **KEEP})
    assert env == dict.fromkeys(SECRETS, "")  # every secret blanked, nothing else touched or added
    assert all(name not in env for name in KEEP)


def test_the_agents_own_anthropic_credentials_survive() -> None:
    assert "ANTHROPIC_API_KEY" not in scrubbed_env(KEEP) and "CLAUDE_CODE_OAUTH_TOKEN" not in scrubbed_env(KEEP)


def test_scrubbing_reads_the_real_environment_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real")
    assert scrubbed_env()["OPENAI_API_KEY"] == ""
    assert "PATH" not in scrubbed_env()


# ------------------------------------------------------------- the options


def test_options_carry_cwd_model_budget_turns_and_isolation(workspace: Path) -> None:
    settings = settings_with(implementer_model="claude-x", implementer_max_task_usd=3.5)
    options = build_agent_options(workspace=workspace, settings=settings, max_turns=7)
    assert Path(str(options.cwd)) == workspace
    assert options.model == "claude-x" and options.max_turns == 7 and options.max_budget_usd == 3.5
    assert options.permission_mode == "acceptEdits"  # the new, safer default
    assert options.setting_sources == []  # never load user/project settings an agent could have written
    assert {"WebFetch", "WebSearch"} <= set(options.disallowed_tools)


def test_the_budget_and_agents_can_be_overridden_per_call(workspace: Path) -> None:
    sentinel = {"backend": object()}
    options = build_agent_options(
        workspace=workspace, settings=settings_with(), max_turns=3, agents=sentinel, max_budget_usd=1.25
    )
    assert options.max_budget_usd == 1.25 and options.agents is sentinel
    assert build_agent_options(workspace=workspace, settings=settings_with(), max_turns=3).agents is None


def test_the_os_sandbox_confines_shell_commands(workspace: Path) -> None:
    domains = ["pypi.org", "registry.npmjs.org"]
    options = build_agent_options(
        workspace=workspace, settings=settings_with(implementer_allowed_domains=domains), max_turns=5
    )
    sandbox = options.sandbox
    assert sandbox["enabled"] is True
    assert sandbox["autoAllowBashIfSandboxed"] is True
    assert sandbox["allowUnsandboxedCommands"] is False  # no escape hatch via dangerouslyDisableSandbox
    assert sandbox["network"]["allowedDomains"] == domains


def test_the_sandbox_denies_reading_credentials_and_the_orchestrators_dotenv(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    denied = build_agent_options(workspace=workspace, settings=settings_with(), max_turns=5).sandbox["filesystem"]["denyRead"]
    home = tmp_path / "home"
    for secret in (".ssh", ".aws", ".gnupg", ".config/gh", ".netrc", ".kube"):
        assert str(home / secret) in denied, secret
    assert str(tmp_path / ".env") in denied


def test_the_pre_tool_use_hook_is_registered_for_the_guarded_tools_and_bound_to_the_workspace(
    workspace: Path,
) -> None:
    options = build_agent_options(workspace=workspace, settings=settings_with(), max_turns=5)
    (matcher,) = options.hooks["PreToolUse"]
    for tool in ("Bash", "Write", "Edit", "Read", "MultiEdit"):
        assert tool in matcher.matcher.split("|")
    (hook,) = matcher.hooks
    import asyncio

    denied = asyncio.run(hook({"tool_name": "Read", "tool_input": {"file_path": "/etc/passwd"}, "cwd": "/"}, "1", {}))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    allowed = asyncio.run(
        hook({"tool_name": "Read", "tool_input": {"file_path": "x.py"}, "cwd": "/somewhere/else"}, "2", {})
    )
    assert allowed == {}  # relative path resolved against the bound workspace, not the reported cwd


def test_the_agent_environment_has_secrets_blanked(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp")
    env = build_agent_options(workspace=workspace, settings=settings_with(), max_turns=5).env
    assert env["OPENAI_API_KEY"] == "" and env["GITHUB_TOKEN"] == ""
    assert "ANTHROPIC_API_KEY" not in env


def test_bypass_permissions_stays_selectable(workspace: Path) -> None:
    options = build_agent_options(
        workspace=workspace, settings=settings_with(implementer_permission_mode="bypassPermissions"), max_turns=5
    )
    assert options.permission_mode == "bypassPermissions"
    assert options.sandbox["enabled"] is True and options.hooks  # still sandboxed and guarded


# ---------------------------------------------------------- sandbox modes


def test_sandbox_off_builds_no_sandbox_but_keeps_the_guard_and_says_so_loudly(
    workspace: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        options = build_agent_options(
            workspace=workspace, settings=Settings(_env_file=None, implementer_sandbox="off"), max_turns=5
        )
    assert options.sandbox is None and options.hooks
    assert "sandbox" in caplog.text.lower() and "off" in caplog.text.lower()


def test_auto_falls_back_with_a_loud_warning_when_the_platform_has_no_sandbox(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(opts, "sandbox_supported", lambda *a, **k: False)
    with caplog.at_level(logging.WARNING):
        options = build_agent_options(
            workspace=workspace, settings=Settings(_env_file=None, implementer_sandbox="auto"), max_turns=5
        )
    assert options.sandbox is None and options.hooks
    assert "not supported" in caplog.text and "container" in caplog.text


def test_auto_enables_the_sandbox_where_it_is_supported(workspace: Path) -> None:
    options = build_agent_options(
        workspace=workspace, settings=Settings(_env_file=None, implementer_sandbox="auto"), max_turns=5
    )
    assert options.sandbox and options.sandbox["enabled"] is True


def test_on_refuses_to_run_without_sandbox_support(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(opts, "sandbox_supported", lambda *a, **k: False)
    with pytest.raises(SandboxUnavailableError, match="IMPLEMENTER_SANDBOX"):
        build_agent_options(workspace=workspace, settings=settings_with(), max_turns=5)


@pytest.mark.parametrize(
    ("platform", "tools", "expected"),
    [
        ("darwin", {"sandbox-exec"}, True),
        ("darwin", set(), False),
        ("linux", {"bwrap", "socat"}, True),
        ("linux", {"bwrap"}, False),
        ("linux", set(), False),
        ("win32", {"bwrap", "socat", "sandbox-exec"}, False),
    ],
)
def test_sandbox_support_per_platform(platform: str, tools: set[str], expected: bool, monkeypatch) -> None:
    monkeypatch.undo()  # drop the autouse "always supported" patch
    assert sandbox_supported(platform=platform, which=lambda name: f"/bin/{name}" if name in tools else None) is expected


# ----------------------------------------------------------------- status


def test_the_status_states_the_truth_for_the_implement_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    on = sandbox_status(settings_with(implementer_allowed_domains=["pypi.org"]))
    assert on.enabled and "pypi.org" in on.summary and "workspace" in on.summary
    off = sandbox_status(Settings(_env_file=None, implementer_sandbox="off"))
    assert not off.enabled and "OFF" in off.summary
    monkeypatch.setattr(opts, "sandbox_supported", lambda *a, **k: False)
    unsupported = sandbox_status(Settings(_env_file=None, implementer_sandbox="auto"))
    assert not unsupported.enabled and "not supported" in unsupported.summary
    demo = sandbox_status(Settings(_env_file=None, demo_mode=True))
    assert not demo.enabled and "demo" in demo.summary.lower()


def test_scrubbed_env_never_blanks_variables_it_does_not_recognise_as_secrets() -> None:
    assert scrubbed_env({"EDITOR": "vim", "TERM": "xterm", "KEYBOARD_LAYOUT": "us", "TOKENIZERS_PARALLELISM": "false"}) == {}

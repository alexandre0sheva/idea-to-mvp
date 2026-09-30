import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from idea_to_mvp import cli
from idea_to_mvp.config import Settings
from idea_to_mvp.doctor import collect_checks, format_report, run_doctor
from idea_to_mvp.observability import apply_tracing_env
from idea_to_mvp.roles import ROLES


class AuthenticationError(Exception):
    pass


def _runtime_for(role: str):
    spec = ROLES[role]
    settings = Settings(_env_file=None)
    return SimpleNamespace(
        provider=getattr(settings, spec.provider_setting), model=getattr(settings, spec.model_setting)
    )


def _settings(**overrides) -> Settings:
    base = {"openai_api_key": "o", "anthropic_api_key": "a", "google_api_key": "g"}
    return Settings(_env_file=None, **{**base, **overrides})


def _by_name(checks):
    return {c.name.split(" ")[0]: c for c in checks}


def test_all_roles_ok_when_pings_succeed() -> None:
    pings: list[str] = []

    def ping(runtime) -> str:
        pings.append(f"{runtime.provider}:{runtime.model}")
        return "OK"

    checks = collect_checks(_settings(), runtime_for=_runtime_for, ping=ping)
    by_role = _by_name(checks)
    roles = [role for role in ROLES if role != "researcher"]  # only checked when research is enabled
    assert all(by_role[role].status == "ok" for role in roles)
    # summarizer, architect, and strategy share one provider/model: pinged once, reported for each role
    assert len(pings) == len(set(pings)) < len(roles)


def test_failing_ping_reports_fail_with_the_reason_and_does_not_raise() -> None:
    def ping(runtime) -> str:
        if runtime.provider == "google":
            raise AuthenticationError("API key not valid")
        return "OK"

    checks = _by_name(collect_checks(_settings(), runtime_for=_runtime_for, ping=ping))
    assert checks["skeptic"].status == "fail"
    assert "AuthenticationError" in checks["skeptic"].detail and "API key not valid" in checks["skeptic"].detail
    assert checks["pm"].status == "ok"


def test_empty_reply_counts_as_a_failure() -> None:
    checks = _by_name(collect_checks(_settings(), runtime_for=_runtime_for, ping=lambda r: ""))
    assert checks["pm"].status == "fail" and "no text" in checks["pm"].detail


def test_missing_key_fails_without_pinging() -> None:
    pinged: list[str] = []
    settings = _settings(google_api_key=None)
    checks = _by_name(collect_checks(settings, runtime_for=_runtime_for, ping=lambda r: pinged.append(r.provider) or "OK"))
    assert checks["skeptic"].status == "fail" and "GOOGLE_API_KEY" in checks["skeptic"].detail
    assert "google" not in pinged


def test_offline_mode_only_checks_configuration() -> None:
    def ping(runtime) -> str:
        raise AssertionError("must not call a model offline")

    checks = collect_checks(_settings(), runtime_for=_runtime_for, ping=ping, offline=True)
    assert all(c.status in ("ok", "warn") for c in checks)
    checks = collect_checks(_settings(openai_api_key=None), runtime_for=_runtime_for, ping=ping, offline=True)
    assert _by_name(checks)["pm"].status == "fail"


def test_demo_mode_needs_no_keys_or_pings() -> None:
    settings = Settings(_env_file=None, demo_mode=True)
    checks = collect_checks(settings, runtime_for=_runtime_for, ping=lambda r: (_ for _ in ()).throw(AssertionError()))
    assert [c.status for c in checks] == ["ok"] and "demo" in checks[0].name.lower()


def test_implementer_checks_the_bundled_cli_and_warns_without_an_api_key() -> None:
    checks = collect_checks(_settings(anthropic_api_key=None), runtime_for=_runtime_for, ping=lambda r: "OK", offline=True)
    implementer = [c for c in checks if c.name.startswith("implementer")]
    assert implementer and all(c.status != "fail" for c in implementer)  # bundled CLI ships with the SDK
    assert any(c.status == "warn" and "ANTHROPIC_API_KEY" in c.detail for c in implementer)


def test_run_doctor_exit_code_and_report(capsys: pytest.CaptureFixture[str]) -> None:
    def failing(runtime) -> str:
        raise AuthenticationError("nope")

    assert run_doctor(_settings(), runtime_for=_runtime_for, ping=failing) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "AuthenticationError" in out
    assert run_doctor(_settings(), runtime_for=_runtime_for, ping=lambda r: "OK") == 0
    assert "FAIL" not in capsys.readouterr().out


def test_format_report_is_a_readable_table() -> None:
    checks = collect_checks(_settings(), runtime_for=_runtime_for, ping=lambda r: "OK")
    report = format_report(checks)
    assert report.count("\n") >= len(ROLES) and "pm" in report and "gpt-" in report


# ----------------------------------------------------------------------------- cli


def test_cli_doctor_offline_in_demo_mode(demo_env: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(demo_env)  # never pick up the developer's real .env
    assert cli.main(["doctor", "--offline"]) == 0
    assert "demo" in capsys.readouterr().out.lower()


def test_cli_doctor_offline_fails_without_keys(tmp_path: Path, monkeypatch, capsys) -> None:
    from idea_to_mvp.config import clear_settings_cache

    monkeypatch.chdir(tmp_path)
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "DEMO_MODE"):
        monkeypatch.delenv(key, raising=False)
    clear_settings_cache()
    try:
        assert cli.main(["doctor", "--offline"]) == 1
    finally:
        clear_settings_cache()
    assert "FAIL" in capsys.readouterr().out


def test_cli_without_arguments_launches_the_ui(monkeypatch) -> None:
    launched: list[bool] = []
    monkeypatch.setattr("idea_to_mvp.ui.app.main", lambda: launched.append(True))
    assert cli.main([]) == 0 and launched == [True]


# ------------------------------------------------------------------------ tracing env


def test_apply_tracing_env_exports_only_langsmith_variables(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LANGSMITH_TRACING=true\nLANGSMITH_API_KEY=ls-secret\nOPENAI_API_KEY=sk-should-not-leak\nOTHER=1\n"
    )
    for key in ("LANGSMITH_TRACING", "LANGSMITH_API_KEY", "OPENAI_API_KEY", "OTHER"):
        monkeypatch.delenv(key, raising=False)
    applied = apply_tracing_env(env_file)
    assert applied == ["LANGSMITH_API_KEY", "LANGSMITH_TRACING"]
    assert os.environ["LANGSMITH_TRACING"] == "true"
    assert "OPENAI_API_KEY" not in os.environ and "OTHER" not in os.environ
    monkeypatch.delenv("LANGSMITH_TRACING")
    monkeypatch.delenv("LANGSMITH_API_KEY")


def test_apply_tracing_env_never_overrides_the_real_environment(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("LANGSMITH_PROJECT=from-file\n")
    monkeypatch.setenv("LANGSMITH_PROJECT", "from-shell")
    assert apply_tracing_env(env_file) == []
    assert os.environ["LANGSMITH_PROJECT"] == "from-shell"


def test_apply_tracing_env_tolerates_a_missing_file(tmp_path: Path) -> None:
    assert apply_tracing_env(tmp_path / "nope.env") == []


def test_the_researcher_is_checked_only_when_research_is_enabled() -> None:
    def ping(runtime) -> str:
        return "OK"

    off = _by_name(collect_checks(_settings(), runtime_for=_runtime_for, ping=ping))
    on = _by_name(collect_checks(_settings(enable_research=True), runtime_for=_runtime_for, ping=ping))
    assert "researcher" not in off and on["researcher"].status == "ok"

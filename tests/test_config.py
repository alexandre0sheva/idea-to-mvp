from pathlib import Path

import pytest
from dotenv import dotenv_values
from pydantic import ValidationError

from idea_to_mvp.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


def test_env_example_matches_settings_defaults(monkeypatch) -> None:
    """Every KEY=value in .env.example must equal the corresponding Settings default."""
    values = {k: v for k, v in dotenv_values(ENV_EXAMPLE).items() if v not in (None, "")}
    for key in values:
        monkeypatch.delenv(key, raising=False)
    defaults = Settings(_env_file=None)
    checked = 0
    for key, raw in values.items():
        field = key.lower()
        if field.endswith("_api_key") or field not in Settings.model_fields:
            continue
        expected = getattr(defaults, field)
        if isinstance(expected, bool):
            actual: object = raw.lower() == "true"
        elif isinstance(expected, Path):
            actual = Path(raw).expanduser()
        else:
            actual = type(expected)(raw)
        assert actual == expected, f"{key}: .env.example={raw!r} but Settings default={expected!r}"
        checked += 1
    assert checked >= 15


def test_every_model_setting_is_documented_in_env_example() -> None:
    documented = {k.lower() for k in dotenv_values(ENV_EXAMPLE)}
    for field in Settings.model_fields:
        if field.endswith("_model") or field.endswith("_provider"):
            assert field in documented, f"{field.upper()} missing from .env.example"


def test_panel_and_concurrency_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.panel_mode == "moderated"
    assert settings.llm_max_concurrency == 4
    assert settings.moderator_provider == "anthropic" and settings.moderator_model.startswith("claude-haiku")


def test_blueprint_review_settings() -> None:
    settings = Settings(_env_file=None)
    assert settings.max_blueprint_revisions == 1 and settings.plan_max_tokens >= 4000
    assert Settings(_env_file=None, max_blueprint_revisions=0).max_blueprint_revisions == 0
    with pytest.raises(ValidationError):
        Settings(_env_file=None, max_blueprint_revisions=-1)


def test_panel_settings_are_validated() -> None:
    assert Settings(_env_file=None, panel_mode="round_robin").panel_mode == "round_robin"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, panel_mode="free_for_all")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_max_concurrency=0)


def test_implementer_safety_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.implementer_sandbox == "auto"
    assert settings.implementer_permission_mode == "acceptEdits"
    assert {"pypi.org", "files.pythonhosted.org", "registry.npmjs.org", "github.com"} <= set(
        settings.implementer_allowed_domains
    )


def test_allowed_domains_can_be_given_as_a_comma_separated_list(monkeypatch) -> None:
    monkeypatch.setenv("IMPLEMENTER_ALLOWED_DOMAINS", "pypi.org, example.com ,, crates.io")
    assert Settings(_env_file=None).implementer_allowed_domains == ["pypi.org", "example.com", "crates.io"]
    monkeypatch.setenv("IMPLEMENTER_ALLOWED_DOMAINS", "")
    assert Settings(_env_file=None).implementer_allowed_domains == []


def test_the_sandbox_setting_is_validated() -> None:
    assert Settings(_env_file=None, implementer_sandbox="off").implementer_sandbox == "off"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, implementer_sandbox="maybe")


def test_env_example_documents_the_allowed_domains_default() -> None:
    text = ENV_EXAMPLE.read_text()
    assert "IMPLEMENTER_SANDBOX=" in text and "IMPLEMENTER_ALLOWED_DOMAINS=" in text
    domains = next(line for line in text.splitlines() if line.lstrip("# ").startswith("IMPLEMENTER_ALLOWED_DOMAINS="))
    listed = domains.split("=", 1)[1].split(",")
    assert [d.strip() for d in listed] == Settings(_env_file=None).implementer_allowed_domains


def test_implementer_budget_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.implementer_max_total_usd == 25.0
    assert settings.implementer_max_task_usd == 5.0
    assert settings.implementer_max_task_turns == 40


def test_the_old_budget_variable_still_sets_the_per_task_cap(monkeypatch) -> None:
    monkeypatch.setenv("IMPLEMENTER_MAX_BUDGET_USD", "7.5")
    assert Settings(_env_file=None).implementer_max_task_usd == 7.5
    monkeypatch.setenv("IMPLEMENTER_MAX_TASK_USD", "3")
    assert Settings(_env_file=None).implementer_max_task_usd == 3.0  # the new name wins


def test_budgets_and_turns_must_be_positive() -> None:
    for field in ("implementer_max_total_usd", "implementer_max_task_usd", "implementer_max_task_turns"):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **{field: 0})


def test_parallelism_default_and_validation() -> None:
    assert Settings(_env_file=None).implementer_max_parallel == 3
    assert Settings(_env_file=None, implementer_max_parallel=1).implementer_max_parallel == 1
    with pytest.raises(ValidationError):
        Settings(_env_file=None, implementer_max_parallel=0)

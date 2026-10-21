"""The model catalog (`models.toml`): validation, lookups, prices, live listing, and the `models` command."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from idea_to_mvp import cli, model_report
from idea_to_mvp.config import PROFILES, Settings
from idea_to_mvp.doctor import collect_checks
from idea_to_mvp.model_catalog import (
    AGENT_ROLES,
    ModelCatalog,
    ModelEntry,
    accepts_sampling,
    check_models,
    get_catalog,
    list_provider_models,
    load_catalog,
    parse_model_id,
)
from idea_to_mvp.roles import ROLES
from idea_to_mvp.ui.components import model_profile_markdown

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 10, 19)


def entry(model_id: str = "m-1", **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": model_id,
        "provider": "anthropic",
        "tier": "balanced",
        "input_per_mtok": 2.0,
        "output_per_mtok": 10.0,
        "cached_input_per_mtok": 0.2,
        "context_window": 1_000_000,
        "max_output": 128_000,
        "structured_output": True,
        "sampling": False,
        "web_search": True,
        "checked": "2026-10-19",
        "source": "https://example.com/pricing",
    }
    return {**base, **overrides}


def catalog_data(models: list[dict[str, Any]] | None = None, **overrides: Any) -> dict[str, Any]:
    models = models if models is not None else [entry("m-1"), entry("m-2", tier="fast")]
    roles = {"pm": models[0]["id"], "architect": models[-1]["id"]}
    data: dict[str, Any] = {
        "models": models,
        "profiles": {"fast": dict(roles), "balanced": dict(roles), "quality": dict(roles)},
        "typical_run": {"pm": {"calls": 2, "input_tokens": 1000, "output_tokens": 100}},
    }
    return {**data, **overrides}


# ---------------------------------------------------------------- the packaged file


def test_the_packaged_catalog_loads_and_every_entry_is_well_formed() -> None:
    catalog = get_catalog()
    assert len(catalog.models) >= 6
    for item in catalog.models.values():
        assert item.source.startswith("https://")
        assert item.checked <= date.today()
        assert item.context_window > 0 and item.max_output > 0


def test_every_model_a_setting_or_an_example_names_is_in_the_catalog() -> None:
    catalog = get_catalog()
    settings = Settings(_env_file=None)
    names = {value for key, value in settings.model_dump().items() if key.endswith("_model")}
    names |= {model for profile in PROFILES.values() for _, model in profile.values()}
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    names |= set(re.findall(r"^#?\s*[A-Z_]*MODEL=(\S+)", example, flags=re.MULTILINE))
    assert {name for name in names if catalog.find(name) is None} == set()


def test_the_profiles_come_from_the_catalog_with_each_models_own_provider() -> None:
    catalog = get_catalog()
    for name, choices in PROFILES.items():
        assert choices == catalog.profile_choices(name)
        for provider, model in choices.values():
            found = catalog.find(model)
            assert found is not None and found.provider == provider


def test_the_typical_run_covers_the_planning_roles_and_the_agent_sessions() -> None:
    catalog = get_catalog()
    profile_roles = {spec.provider_setting.removesuffix("_provider") for spec in ROLES.values()}
    assert set(catalog.typical_run) <= profile_roles | set(AGENT_ROLES)
    assert {
        "pm",
        "tech_lead",
        "skeptic",
        "moderator",
        "summarizer",
        "architect",
        "implementer",
        "verifier",
    } <= set(catalog.typical_run)


def test_the_catalog_file_is_shipped_with_the_package() -> None:
    assert (ROOT / "src" / "idea_to_mvp" / "models.toml").is_file()
    assert load_catalog().models == get_catalog().models


# ------------------------------------------------------------------- validation


def test_a_well_formed_catalog_validates() -> None:
    catalog = ModelCatalog.model_validate(catalog_data())
    assert set(catalog.models) == {"m-1", "m-2"} and catalog.profile_choices("fast")["pm"] == (
        "anthropic",
        "m-1",
    )


def test_duplicate_model_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        ModelCatalog.model_validate(catalog_data([entry("m-1"), entry("m-1")]))


def test_an_alias_that_collides_with_another_model_is_rejected() -> None:
    with pytest.raises(ValidationError, match="alias"):
        ModelCatalog.model_validate(catalog_data([entry("m-1"), entry("m-2", aliases=["m-1"])]))


def test_a_profile_naming_an_unknown_model_is_rejected() -> None:
    data = catalog_data()
    data["profiles"]["fast"]["pm"] = "nope"
    with pytest.raises(ValidationError, match="nope"):
        ModelCatalog.model_validate(data)


def test_profiles_must_cover_the_same_roles() -> None:
    data = catalog_data()
    del data["profiles"]["quality"]["architect"]
    with pytest.raises(ValidationError, match="same roles"):
        ModelCatalog.model_validate(data)


def test_a_price_needs_both_input_and_output_and_cannot_be_negative() -> None:
    with pytest.raises(ValidationError, match="both"):
        ModelEntry.model_validate(entry(output_per_mtok=None))
    with pytest.raises(ValidationError):
        ModelEntry.model_validate(entry(input_per_mtok=-1))


def test_a_model_may_be_unpriced() -> None:
    unpriced = ModelEntry.model_validate(
        entry(input_per_mtok=None, output_per_mtok=None, cached_input_per_mtok=None)
    )
    assert not unpriced.priced and unpriced.cost(1000, 1000) is None


def test_a_source_must_be_a_web_address() -> None:
    with pytest.raises(ValidationError, match="http"):
        ModelEntry.model_validate(entry(source="pricing page"))


# ------------------------------------------------------------- lookups and prices


def test_a_model_is_found_by_id_or_alias_and_unknown_ids_are_not_found() -> None:
    catalog = ModelCatalog.model_validate(
        catalog_data([entry("m-1", aliases=["m-latest"]), entry("m-2")])
    )
    assert catalog.find("m-1") is catalog.find("m-latest") and catalog.find("  m-1 ") is not None
    assert catalog.find("other") is None and catalog.find("") is None


def test_cost_prices_input_output_and_cached_input_per_million_tokens() -> None:
    item = ModelEntry.model_validate(entry())  # $2 in, $10 out, $0.2 cached
    assert item.cost(1_000_000, 0) == pytest.approx(2.0)
    assert item.cost(0, 1_000_000) == pytest.approx(10.0)
    assert item.cost(1_000_000, 0, cached_tokens=600_000) == pytest.approx(0.4 * 2.0 + 0.6 * 0.2)


def test_cached_tokens_cost_the_full_price_when_the_catalog_has_no_cached_rate() -> None:
    item = ModelEntry.model_validate(entry(cached_input_per_mtok=None))
    assert item.cost(1_000_000, 0, cached_tokens=500_000) == pytest.approx(2.0)


def test_the_estimated_run_cost_follows_the_models_a_profile_chooses() -> None:
    catalog = get_catalog()
    fast, balanced, quality = (
        catalog.estimate_run(name) for name in ("fast", "balanced", "quality")
    )
    assert 0 < fast.planning < balanced.planning <= quality.planning
    assert 0 < fast.agents < balanced.agents < quality.agents
    assert (
        balanced.total == pytest.approx(balanced.planning + balanced.agents)
        and not balanced.unpriced
    )


def test_a_model_given_for_a_role_replaces_the_profiles_choice_in_the_estimate() -> None:
    catalog = get_catalog()
    balanced = catalog.estimate_run("balanced")
    pinned = catalog.estimate_run("balanced", {"implementer": "claude-opus-5-5"})
    assert pinned.agents > balanced.agents and pinned.planning == balanced.planning
    assert catalog.estimate_run("balanced", {"implementer": "homemade"}).unpriced == ["implementer"]


def test_agent_sessions_count_as_agents_and_the_rest_as_planning() -> None:
    data = catalog_data(
        typical_run={
            "pm": {"calls": 1, "input_tokens": 1_000_000, "output_tokens": 0},
            "verifier": {"calls": 1, "input_tokens": 0, "output_tokens": 1_000_000},
        }
    )
    estimate = ModelCatalog.model_validate(data).estimate_run("fast", {"verifier": "m-1"})
    assert estimate.planning == pytest.approx(2.0) and estimate.agents == pytest.approx(10.0)


def test_an_unpriced_model_is_listed_and_left_out_of_the_estimate() -> None:
    data = catalog_data(
        [
            entry("m-1", input_per_mtok=None, output_per_mtok=None, cached_input_per_mtok=None),
            entry("m-2"),
        ]
    )
    estimate = ModelCatalog.model_validate(data).estimate_run("fast")
    assert estimate.unpriced == ["pm"] and estimate.planning == 0


# ------------------------------------------------------------------- sampling


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "claude-haiku-5-5",
        "gemini-3.8-flash",
        "gpt-6.1-sol",
        "gpt-6-luna",
    ],
)
def test_a_catalogued_model_that_rejects_sampling_is_known_from_the_catalog(model: str) -> None:
    assert get_catalog().find(model) is not None and not accepts_sampling(model)


def test_a_catalogued_model_that_accepts_sampling_and_its_alias() -> None:
    catalog = ModelCatalog.model_validate(
        catalog_data([entry("m-1", aliases=["m-latest"], sampling=True)])
    )
    assert accepts_sampling("m-1", catalog) and accepts_sampling("m-latest", catalog)


def test_the_catalog_decides_before_the_name_pattern_does() -> None:
    catalog = ModelCatalog.model_validate(
        catalog_data(
            [entry("gpt-5-custom", sampling=True), entry("claude-opus-5-9", sampling=False)]
        )
    )
    assert accepts_sampling("gpt-5-custom", catalog) and not accepts_sampling(
        "claude-opus-5-9", catalog
    )


@pytest.mark.parametrize(
    "model", ["claude-opus-4-8", "claude-fable-5", "gemini-3-flash-preview", "gpt-6.9-x"]
)
def test_an_unlisted_model_falls_back_to_the_name_pattern_for_current_generations(
    model: str,
) -> None:
    assert get_catalog().find(model) is None and not accepts_sampling(model)


@pytest.mark.parametrize("model", ["claude-sonnet-4-6", "gpt-4o-mini", ""])
def test_an_unlisted_older_model_accepts_sampling(model: str) -> None:
    assert accepts_sampling(model)


# ------------------------------------------------------------ id parsing and checks


@pytest.mark.parametrize(
    ("model_id", "family", "version"),
    [
        ("claude-sonnet-5-5", ("claude", "sonnet"), (5, 5)),
        ("claude-haiku-4-5-20251001", ("claude", "haiku"), (4, 5)),
        ("gpt-6.1-sol", ("gpt", "sol"), (6, 1)),
        ("gpt-6-luna", ("gpt", "luna"), (6,)),
        ("gpt-6-luna-2026-10-01", ("gpt", "luna"), (6,)),
        ("gemini-3.8-flash", ("gemini", "flash"), (3, 8)),
        ("gemini-3-flash-preview", ("gemini", "flash"), (3,)),
    ],
)
def test_a_model_id_splits_into_a_family_and_a_version(
    model_id: str, family: tuple[str, ...], version: tuple[int, ...]
) -> None:
    parsed = parse_model_id(model_id)
    assert (parsed.family, parsed.version) == (family, version)


def test_a_preview_is_flagged_so_it_does_not_count_as_a_newer_release() -> None:
    assert (
        parse_model_id("gemini-3-flash-preview").preview
        and not parse_model_id("gemini-3.8-flash").preview
    )


def configured(*items: tuple[str, str, str]) -> list[tuple[str, str, str]]:
    return list(items)


def test_check_flags_unknown_deprecated_and_outdated_models_and_passes_the_rest() -> None:
    catalog = ModelCatalog.model_validate(
        catalog_data(
            [
                entry("m-1"),
                entry("claude-haiku-4-5-20251001", aliases=["claude-haiku-4-5"], status="legacy"),
                entry("old-model", status="deprecated"),
                entry("retired-model", retires="2026-10-01"),
            ]
        )
    )
    listings = {
        "anthropic": [
            "m-1",
            "claude-haiku-4-5-20251001",
            "claude-haiku-5-5",
            "old-model",
            "retired-model",
        ]
    }
    findings = check_models(
        configured(
            ("pm", "anthropic", "m-1"),
            ("moderator", "anthropic", "claude-haiku-4-5-20251001"),
            ("a", "anthropic", "typo-model"),
            ("b", "anthropic", "old-model"),
            ("c", "anthropic", "retired-model"),
        ),
        listings,
        catalog,
        today=TODAY,
    )
    by_model = {f.model: f for f in findings}
    assert "m-1" not in by_model
    assert (
        by_model["claude-haiku-4-5-20251001"].kind == "outdated"
        and "claude-haiku-5-5" in by_model["claude-haiku-4-5-20251001"].detail
    )
    assert by_model["typo-model"].kind == "unknown"
    assert (
        by_model["old-model"].kind == "deprecated"
        and by_model["retired-model"].kind == "deprecated"
    )
    assert all(f.roles for f in findings)


def test_check_reports_each_model_once_with_every_role_that_uses_it() -> None:
    catalog = ModelCatalog.model_validate(catalog_data())
    findings = check_models(
        configured(("architect", "anthropic", "ghost"), ("strategy", "anthropic", "ghost")),
        {"anthropic": ["m-1"]},
        catalog,
        today=TODAY,
    )
    assert len(findings) == 1 and findings[0].roles == ["architect", "strategy"]


def test_check_accepts_a_model_the_provider_lists_by_its_alias_and_ignores_previews_and_other_families() -> (
    None
):
    catalog = ModelCatalog.model_validate(
        catalog_data([entry("claude-haiku-4-5-20251001", aliases=["claude-haiku-4-5"])])
    )
    listings = {"anthropic": ["claude-haiku-4-5", "claude-haiku-9-0-preview", "claude-opus-9-9"]}
    assert (
        check_models(
            configured(("m", "anthropic", "claude-haiku-4-5-20251001")),
            listings,
            catalog,
            today=TODAY,
        )
        == []
    )


def test_check_marks_a_provider_whose_models_could_not_be_listed() -> None:
    catalog = ModelCatalog.model_validate(catalog_data())
    findings = check_models(
        configured(("pm", "openai", "gpt-6.1-sol"), ("x", "anthropic", "m-1")),
        {"openai": RuntimeError("401 unauthorized"), "anthropic": ["m-1"]},
        catalog,
        today=TODAY,
    )
    assert [(f.kind, f.model) for f in findings] == [
        ("unavailable", "openai")
    ] and "401" in findings[0].detail


# ------------------------------------------------------------------ live listing


def test_listing_anthropic_follows_the_pages_and_keeps_the_key_out_of_the_url() -> None:
    calls: list[tuple[str, dict[str, str]]] = []
    pages = [
        {"data": [{"id": "a"}, {"id": "b"}], "has_more": True, "last_id": "b"},
        {"data": [{"id": "c"}], "has_more": False, "last_id": "c"},
    ]

    def fetch(url: str, headers: dict[str, str]) -> dict[str, Any]:
        calls.append((url, headers))
        return pages[len(calls) - 1]

    assert list_provider_models("anthropic", "sk-secret", fetch) == ["a", "b", "c"]
    assert "after_id=b" in calls[1][0] and all("sk-secret" not in url for url, _ in calls)
    assert calls[0][1]["x-api-key"] == "sk-secret"


def test_listing_openai_reads_the_bearer_protected_list() -> None:
    seen: dict[str, Any] = {}

    def fetch(url: str, headers: dict[str, str]) -> dict[str, Any]:
        seen.update(url=url, headers=headers)
        return {"data": [{"id": "gpt-6.1-sol"}, {"id": "gpt-6-luna"}]}

    assert list_provider_models("openai", "k", fetch) == ["gpt-6.1-sol", "gpt-6-luna"]
    assert seen["headers"]["Authorization"] == "Bearer k" and "openai.com" in seen["url"]


def test_listing_google_strips_the_models_prefix_and_follows_the_page_token() -> None:
    urls: list[str] = []
    pages = [
        {"models": [{"name": "models/gemini-3.8-flash"}], "nextPageToken": "t2"},
        {"models": [{"name": "models/gemini-3-flash-preview"}]},
    ]

    def fetch(url: str, headers: dict[str, str]) -> dict[str, Any]:
        urls.append(url)
        assert headers["x-goog-api-key"] == "g" and "key=" not in url
        return pages[len(urls) - 1]

    assert list_provider_models("google", "g", fetch) == [
        "gemini-3.8-flash",
        "gemini-3-flash-preview",
    ]
    assert "pageToken=t2" in urls[1]


# ------------------------------------------------------------- the `models` command


def test_the_models_report_shows_every_role_for_every_profile_with_prices_and_costs() -> None:
    text = model_report.format_models_report(Settings(_env_file=None))
    for role in ("pm", "tech_lead", "skeptic", "moderator", "summarizer", "architect", "research"):
        assert role in text
    for name in (
        "fast",
        "balanced",
        "quality",
        "claude-sonnet-5-5",
        "gpt-6-luna",
        "gemini-3.8-flash",
    ):
        assert name in text
    assert "$" in text and "typical run" in text.lower()
    for kind in ("implementer", "verifier", "escalation"):
        assert kind in text


def test_the_report_marks_models_that_are_not_in_the_catalog_as_unpriced() -> None:
    text = model_report.format_models_report(
        Settings(_env_file=None, implementer_model="my-agent-model")
    )
    assert "my-agent-model" in text and "unpriced" in text.lower()


def test_cli_models_prints_the_report(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch
) -> None:
    from idea_to_mvp.config import clear_settings_cache

    monkeypatch.chdir(tmp_path)  # never pick up the developer's real .env
    clear_settings_cache()
    try:
        assert cli.main(["models"]) == 0
    finally:
        clear_settings_cache()
    assert "balanced" in capsys.readouterr().out


def fake_listing(monkeypatch, models: dict[str, list[str]]) -> list[str]:
    asked: list[str] = []

    def lister(provider: str, key: str, fetch: Any = None) -> list[str]:
        asked.append(provider)
        return models[provider]

    monkeypatch.setattr(model_report, "list_provider_models", lister)
    return asked


def keyed(**overrides: Any) -> Settings:
    return Settings(
        _env_file=None, openai_api_key="o", anthropic_api_key="a", google_api_key="g", **overrides
    )


def test_models_check_lists_each_provider_once_and_passes_when_nothing_is_wrong(
    monkeypatch,
) -> None:
    catalog = get_catalog()
    asked = fake_listing(
        monkeypatch,
        {
            "openai": ["gpt-6.1-sol", "gpt-6-luna"],
            "anthropic": ["claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5-5"],
            "google": ["gemini-3.8-flash"],
        },
    )
    out: list[str] = []
    code = model_report.run_models(keyed(), check=True, out=out.append, catalog=catalog)
    assert sorted(asked) == ["anthropic", "google", "openai"] and code == 0
    assert "no problems" in "\n".join(out).lower()


def test_models_check_exits_nonzero_for_a_model_the_provider_does_not_list(monkeypatch) -> None:
    fake_listing(
        monkeypatch,
        {
            "openai": ["gpt-6.1-sol"],
            "anthropic": ["claude-opus-5-5"],
            "google": ["gemini-3.8-flash"],
        },
    )
    out: list[str] = []
    assert model_report.run_models(keyed(), check=True, out=out.append) == 1
    assert "unknown" in "\n".join(out).lower()


def test_models_check_without_a_key_says_so_instead_of_calling_the_provider(monkeypatch) -> None:
    asked = fake_listing(monkeypatch, {"openai": [], "anthropic": [], "google": []})
    out: list[str] = []
    model_report.run_models(Settings(_env_file=None), check=True, out=out.append)
    assert asked == [] and "no API key" in "\n".join(out)


def test_cli_models_check_is_opt_in(monkeypatch, tmp_path: Path, capsys) -> None:
    from idea_to_mvp.config import clear_settings_cache

    monkeypatch.chdir(tmp_path)
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    asked = fake_listing(monkeypatch, {"openai": [], "anthropic": [], "google": []})
    clear_settings_cache()
    try:
        assert cli.main(["models"]) == 0 and asked == []
        assert cli.main(["models", "--check"]) in (0, 1)
    finally:
        clear_settings_cache()
    assert "no API key" in capsys.readouterr().out


# ------------------------------------------------------------- doctor and the UI


def doctor_settings(**overrides: Any) -> Settings:
    return keyed(**overrides)


def test_doctor_stays_quiet_about_the_default_models() -> None:
    checks = collect_checks(
        doctor_settings(), runtime_for=lambda role: None, ping=lambda r: "OK", offline=True
    )
    assert [c for c in checks if c.name.startswith("catalog")] == []


def test_doctor_warns_offline_about_a_model_that_is_not_in_the_catalog() -> None:
    checks = collect_checks(
        doctor_settings(architect_model="my-own-model"), ping=lambda r: "OK", offline=True
    )
    warned = [c for c in checks if c.name.startswith("catalog")]
    assert len(warned) == 1 and warned[0].status == "warn" and "my-own-model" in warned[0].name
    assert "unknown" in warned[0].detail.lower()


def test_doctor_warns_about_an_unpriced_model_and_a_deprecated_one() -> None:
    catalog = ModelCatalog.model_validate(
        catalog_data(
            [
                entry(
                    "gpt-6.1-sol",
                    provider="openai",
                    input_per_mtok=None,
                    output_per_mtok=None,
                    cached_input_per_mtok=None,
                ),
                entry("claude-sonnet-5-5", status="deprecated"),
                entry("claude-haiku-5-5"),
                entry("gemini-3.8-flash", provider="google"),
                entry("claude-opus-5-5"),
            ]
        )
    )
    checks = collect_checks(doctor_settings(), ping=lambda r: "OK", offline=True, catalog=catalog)
    details = {c.name: c.detail.lower() for c in checks if c.name.startswith("catalog")}
    assert any("gpt-6.1-sol" in name and "no price" in detail for name, detail in details.items())
    assert any(
        "claude-sonnet-5-5" in name and "deprecated" in detail for name, detail in details.items()
    )


def test_the_settings_accordion_shows_each_models_price(monkeypatch) -> None:
    for key in ("PM_MODEL", "PM_PROVIDER"):
        monkeypatch.delenv(key, raising=False)
    text = model_profile_markdown(Settings(_env_file=None))
    assert "$2.00 / $10.00" in text  # gpt-6.1-sol, per MTok in / out
    assert "unpriced" in model_profile_markdown(
        Settings(_env_file=None, implementer_model="homemade")
    )


def test_the_settings_accordion_shows_every_agent_session_kind_and_each_profiles_typical_run(
    monkeypatch,
) -> None:
    for key in ("IMPLEMENTER_MODEL", "VERIFIER_MODEL", "ESCALATION_MODEL"):
        monkeypatch.delenv(key, raising=False)
    text = model_profile_markdown(Settings(_env_file=None))
    for kind in ("implementer", "verifier", "escalation"):
        assert kind in text
    catalog = get_catalog()
    for name in PROFILES:
        assert f"{name} ${catalog.estimate_run(name).total:.2f}" in text


# ------------------------------------------------------- docs agree with the catalog


def test_the_architecture_docs_model_choices_table_matches_the_balanced_profile() -> None:
    doc = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    section = doc.split("### Model choices", 1)[1].split("\n## ", 1)[0].split("\n### ", 1)[0]
    rows = re.findall(
        r"^\| `([a-z_]+)`[^|]*\| `([^`]+)` \|[^|]*\| ([^|]+) \|$", section, flags=re.MULTILINE
    )
    catalog = get_catalog()
    assert {role for role, _, _ in rows} == set(PROFILES["balanced"])
    for role, model, price in rows:
        assert PROFILES["balanced"][role][1] == model, role
        assert price.strip() == model_report.price_label(catalog.find(model)), role
    checked = max(item.checked for item in catalog.models.values())
    assert f"checked {checked}" in section


def test_the_readme_quotes_each_profiles_typical_run_cost_from_the_catalog() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    catalog = get_catalog()
    for name in PROFILES:
        estimate = catalog.estimate_run(name)
        assert re.search(
            rf"^\| `{name}`[^|]*\|.*\| \${estimate.total:.2f} \|$", readme, flags=re.MULTILINE
        ), name

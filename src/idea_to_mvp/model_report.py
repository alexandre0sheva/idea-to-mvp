"""`idea-to-mvp models`: what every profile runs on and costs, and (opt-in) what the providers list today."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

from idea_to_mvp.config import PROFILES, Settings
from idea_to_mvp.model_catalog import (
    AGENT_ROLE,
    Finding,
    ModelCatalog,
    ModelEntry,
    Provider,
    check_models,
    get_catalog,
    list_provider_models,
)
from idea_to_mvp.roles import ROLES, resolve_role_model

_PROVIDERS: tuple[Provider, ...] = ("openai", "anthropic", "google")
_BLOCKING = {"unknown", "deprecated"}  # `--check` exits 1 for these; "outdated" is advice


def price_label(item: ModelEntry | None) -> str:
    """'$in / $out' per million tokens, or 'unpriced' for a model without a price (or not in the catalog)."""
    if item is None or item.input_per_mtok is None or item.output_per_mtok is None:
        return "unpriced"
    return f"${item.input_per_mtok:.2f} / ${item.output_per_mtok:.2f}"


def configured_models(settings: Settings) -> list[tuple[str, str, str]]:
    """`(role, provider, model)` for every role the settings resolve, then the agent sessions."""
    rows: list[tuple[str, str, str]] = [(role, *resolve_role_model(settings, role)) for role in ROLES]
    rows.append((AGENT_ROLE, "anthropic", settings.implementer_model))
    return rows


def _table(header: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(line[i]) for line in [header, *rows]) for i in range(len(header))]
    lines = ["  ".join(cell.ljust(width) for cell, width in zip(line, widths, strict=True)).rstrip() for line in [header, *rows]]
    lines.insert(1, "  ".join("-" * width for width in widths))
    return "\n".join(lines)


def _dollars(value: float) -> str:
    return f"${value:,.2f}"


def format_models_report(settings: Settings, catalog: ModelCatalog | None = None) -> str:
    """Roles x profiles, the price list, the cost of a typical run per profile, and this configuration's models."""
    catalog = catalog or get_catalog()
    profiles = list(PROFILES)
    roles = list(next(iter(PROFILES.values())))
    checked = max(item.checked for item in catalog.models.values())
    sections = [f"Model catalog: {len(catalog.models)} models (models.toml), last checked {checked}."]

    sections.append(
        "Model per role and profile\n"
        + _table(["role", *profiles], [[role, *(PROFILES[name][role][1] for name in profiles)] for role in roles])
    )

    price_rows = [
        [
            item.id,
            item.provider,
            item.tier,
            item.status,
            price_label(item),
            "-" if item.cached_input_per_mtok is None else f"${item.cached_input_per_mtok:.3f}",
            f"{item.context_window:,}",
            str(item.checked),
        ]
        for item in catalog.models.values()
    ]
    sections.append(
        "Prices, USD per million tokens\n"
        + _table(["model", "provider", "tier", "status", "in / out", "cached in", "context", "checked"], price_rows)
    )

    agent_model = settings.implementer_model
    cost_rows: list[list[str]] = []
    notes: list[str] = []
    for name in profiles:
        estimate = catalog.estimate_run(name, agent_model)
        cost_rows.append(
            [name, _dollars(estimate.planning), _dollars(estimate.agents), _dollars(estimate.total)]
        )
        if estimate.unpriced:
            notes.append(f"{name}: unpriced, left out of the estimate: {', '.join(estimate.unpriced)}")
    sections.append(
        f"Typical run (a three-round panel, a three-task plan; agent sessions on {agent_model}, {price_label(catalog.find(agent_model))})\n"
        + _table(["profile", "planning", "implementer sessions", "total"], cost_rows)
        + ("\n" + "\n".join(notes) if notes else "")
        + "\nEstimates from the token counts in models.toml, not a quote; real runs vary with the idea and the plan."
    )

    configured = [
        [role, provider, model, price_label(catalog.find(model))] for role, provider, model in configured_models(settings)
    ]
    sections.append(
        f"This configuration (profile {settings.model_profile}; roles pinned in .env override it)\n"
        + _table(["role", "provider", "model", "in / out per MTok"], configured)
    )
    return "\n\n".join(sections)


def _format_findings(findings: list[Finding]) -> str:
    if not findings:
        return "Live check: no problems found; every configured model is listed by its provider."
    lines = ["Live check:"]
    for finding in findings:
        roles = f" ({', '.join(finding.roles)})" if finding.roles else ""
        lines.append(f"  {finding.kind.upper():<11} {finding.model}{roles}: {finding.detail}")
    return "\n".join(lines)


def run_models(
    settings: Settings,
    *,
    check: bool = False,
    out: Callable[[str], None] = print,
    catalog: ModelCatalog | None = None,
) -> int:
    """Print the report; with `check`, also list each provider's models (one call per provider with a key).

    Exit code 1 only when a configured model is unknown to its provider or deprecated in the catalog.
    """
    catalog = catalog or get_catalog()
    out(format_models_report(settings, catalog))
    if not check:
        return 0

    configured = configured_models(settings)
    listings: dict[str, list[str] | Exception] = {}
    skipped: list[str] = []
    for provider in _PROVIDERS:
        if not any(p == provider for _, p, _ in configured):
            continue
        key = getattr(settings, f"{provider}_api_key")
        if not key:
            skipped.append(provider)
            continue
        try:
            listings[provider] = list_provider_models(provider, key)
        except Exception as exc:
            listings[provider] = exc
    out("")
    for name in skipped:
        out(f"{name}: no API key set ({name.upper()}_API_KEY), not checked.")
    if not listings:
        out("Live check: nothing to check; set an API key for a provider you use.")
        return 0
    findings = check_models(configured, listings, catalog, today=date.today())
    out(_format_findings(findings))
    return 1 if any(f.kind in _BLOCKING for f in findings) else 0

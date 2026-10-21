"""Token usage tracking: which role spent how many tokens, accumulated in graph state.

Nodes that call a model are wrapped with `@with_usage(role=...)`. The wrapper records every model call
made while the node runs (text *and* structured, retries included) through LangChain's usage callback and
adds `UsageRecord`s to the `usage` state channel, which accumulates across the whole run. A call is priced
from the model catalog (or the `MODEL_PRICES_FILE` override, see `pricing.py`); `cost_usd` stays None for a
model with no price, and the UI calls that "unpriced". Agent SDK sessions report their own dollar cost.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any, TypedDict

from langchain_core.callbacks import get_usage_metadata_callback

from idea_to_mvp import llm
from idea_to_mvp.config import get_settings
from idea_to_mvp.implementation.ledger import AGENT_USAGE_ROLES
from idea_to_mvp.pricing import Price, PricingError, call_cost, settings_overrides

LOGGER = logging.getLogger(__name__)


class UsageRecord(TypedDict):
    role: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None


def _provider_of(role: str) -> str:
    try:
        return str(llm.get_runtime(role).provider)
    except Exception:  # a missing key must not turn a finished node into a failure
        return ""


def _pricing_context() -> tuple[bool, dict[str, Price]]:
    """(demo mode, the prices file's overrides). A bad prices file or settings never fail a finished node."""
    try:
        settings = get_settings()
        if settings.demo_mode:
            return True, {}
        return False, settings_overrides(settings)
    except PricingError as exc:
        LOGGER.warning("MODEL_PRICES_FILE ignored, using the catalog prices: %s", exc)
    except Exception:  # settings that cannot be read are the node's problem, not the usage record's
        LOGGER.debug("Could not read the settings to price model calls.", exc_info=True)
    return False, {}


def _cost(
    model: str, usage: Mapping[str, Any], demo: bool, overrides: dict[str, Price]
) -> float | None:
    """Dollars for the calls of one model (a demo run calls no model, so it costs nothing)."""
    if demo:
        return 0.0
    details = usage.get("input_token_details") or {}
    cached = int(details.get("cache_read") or 0) if isinstance(details, Mapping) else 0
    return call_cost(
        model,
        int(usage.get("input_tokens") or 0),
        int(usage.get("output_tokens") or 0),
        cached,
        overrides=overrides,
    )


def with_usage(
    role: str | Callable[[Mapping[str, Any]], str],
) -> Callable[[Callable[..., dict[str, Any]]], Callable[..., dict[str, Any]]]:
    """Record model usage made during a node under `role` (a role key, or a function of the state)."""

    def decorate(node: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        @functools.wraps(node)
        def wrapper(state: Mapping[str, Any], *args: Any, **kwargs: Any) -> dict[str, Any]:
            role_key = role(state) if callable(role) else role
            with recording_usage(role_key) as records:
                update = node(state, *args, **kwargs)
            return {**update, "usage": records} if records else update

        return wrapper

    return decorate


@contextmanager
def recording_usage(role: str) -> Iterator[list[UsageRecord]]:
    """Collect the model calls made inside the block as priced `UsageRecord`s under `role`; the list is filled when
    the block ends. For a node that needs its own spend (harness tuning writes it to the ledger); every other node
    uses `with_usage`."""
    records: list[UsageRecord] = []
    with get_usage_metadata_callback() as callback:
        yield records
    if not callback.usage_metadata:
        return
    provider = _provider_of(role)
    demo, overrides = _pricing_context()
    records += [
        {
            "role": role,
            "provider": provider,
            "model": model,
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
            "cost_usd": _cost(model, usage, demo, overrides),
        }
        for model, usage in callback.usage_metadata.items()
    ]


def summarize_usage(records: list[UsageRecord]) -> dict[str, Any]:
    """Totals of a run's usage. `agent_cost_usd` (sessions: tasks, resolvers, lanes, fixes) is what the whole-run
    budget counts; `planning_cost_usd` (panel, research, summary, architecture, blueprint, critic calls) is None
    when none of those calls could be priced, and `unpriced_calls` counts the ones that could not.
    `cost_usd` is everything that could be priced."""
    by_role: dict[str, dict[str, Any]] = {}
    cost_total: float | None = None
    planning: float | None = None
    agent = 0.0
    agent_calls = 0
    unpriced = 0
    for record in records:
        row = by_role.setdefault(
            record["role"], {"input_tokens": 0, "output_tokens": 0, "calls": 0, "cost_usd": None}
        )
        row["input_tokens"] += record["input_tokens"]
        row["output_tokens"] += record["output_tokens"]
        row["calls"] += 1
        is_agent = record["role"] in AGENT_USAGE_ROLES
        agent_calls += is_agent
        if record["cost_usd"] is None:
            unpriced += not is_agent
            continue
        row["cost_usd"] = (row["cost_usd"] or 0.0) + record["cost_usd"]
        cost_total = (cost_total or 0.0) + record["cost_usd"]
        if is_agent:
            agent += record["cost_usd"]
        else:
            planning = (planning or 0.0) + record["cost_usd"]
    input_tokens = sum(r["input_tokens"] for r in records)
    output_tokens = sum(r["output_tokens"] for r in records)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "calls": len(records),
        "cost_usd": cost_total,
        "planning_cost_usd": planning,
        "agent_cost_usd": agent,
        "agent_calls": agent_calls,
        "unpriced_calls": unpriced,
        "by_role": by_role,
    }

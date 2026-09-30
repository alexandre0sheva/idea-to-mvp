"""Token usage tracking: which role spent how many tokens, accumulated in graph state.

Nodes that call a model are wrapped with `@with_usage(role=...)`. The wrapper records every model call
made while the node runs (text *and* structured, retries included) through LangChain's usage callback and
adds `UsageRecord`s to the `usage` state channel, which accumulates across the whole run. Agent SDK
sessions (implementation stage) report a dollar cost instead; `cost_usd` is None for plain model calls
because prices go stale and are not hard-coded.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping
from typing import Any, TypedDict

from langchain_core.callbacks import get_usage_metadata_callback

from idea_to_mvp import llm


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


def with_usage(
    role: str | Callable[[Mapping[str, Any]], str],
) -> Callable[[Callable[..., dict[str, Any]]], Callable[..., dict[str, Any]]]:
    """Record model usage made during a node under `role` (a role key, or a function of the state)."""

    def decorate(node: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        @functools.wraps(node)
        def wrapper(state: Mapping[str, Any], *args: Any, **kwargs: Any) -> dict[str, Any]:
            role_key = role(state) if callable(role) else role
            with get_usage_metadata_callback() as callback:
                update = node(state, *args, **kwargs)
            if not callback.usage_metadata:
                return update
            provider = _provider_of(role_key)
            records: list[UsageRecord] = [
                {
                    "role": role_key,
                    "provider": provider,
                    "model": model,
                    "input_tokens": int(usage.get("input_tokens") or 0),
                    "output_tokens": int(usage.get("output_tokens") or 0),
                    "cost_usd": None,
                }
                for model, usage in callback.usage_metadata.items()
            ]
            return {**update, "usage": records}

        return wrapper

    return decorate


def summarize_usage(records: list[UsageRecord]) -> dict[str, Any]:
    by_role: dict[str, dict[str, Any]] = {}
    cost_total: float | None = None
    for record in records:
        row = by_role.setdefault(
            record["role"], {"input_tokens": 0, "output_tokens": 0, "calls": 0, "cost_usd": None}
        )
        row["input_tokens"] += record["input_tokens"]
        row["output_tokens"] += record["output_tokens"]
        row["calls"] += 1
        if record["cost_usd"] is not None:
            row["cost_usd"] = (row["cost_usd"] or 0.0) + record["cost_usd"]
            cost_total = (cost_total or 0.0) + record["cost_usd"]
    input_tokens = sum(r["input_tokens"] for r in records)
    output_tokens = sum(r["output_tokens"] for r in records)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "calls": len(records),
        "cost_usd": cost_total,
        "by_role": by_role,
    }

"""Retry policy for pipeline nodes that call a language model.

Provider SDKs already retry HTTP-level failures (`LLM_MAX_RETRIES`); this is the second layer: if a
node still fails with a *transient* error (rate limit, timeout, connection reset, 5xx) LangGraph
re-runs that node with backoff instead of failing the whole stage. Permanent errors (bad key, invalid
request, schema failures) are never retried.
"""

from __future__ import annotations

from langgraph.types import RetryPolicy

# Class-name fragments (matched against the whole MRO, lowercased) that mark a transient failure.
# Name-based on purpose: no provider-specific imports, works for openai/anthropic/google/httpx alike.
_TRANSIENT_NAME_MARKERS = (
    "ratelimit",
    "toomanyrequests",
    "timeout",
    "timedout",
    "connection",
    "overloaded",
    "unavailable",
    "internalserver",
)
_TRANSIENT_STATUS = frozenset({408, 409, 429})


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError | ConnectionError):
        return True
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        return status in _TRANSIENT_STATUS or status >= 500
    names = " ".join(cls.__name__.lower() for cls in type(exc).__mro__)
    return any(marker in names for marker in _TRANSIENT_NAME_MARKERS)


LLM_RETRY = RetryPolicy(
    initial_interval=2.0,
    backoff_factor=2.0,
    max_attempts=3,
    retry_on=is_transient,
)


def error_hint(stage: str, exc: Exception) -> str:
    """One line telling the user why `stage` failed and what to try."""
    name = type(exc).__name__.lower()
    if "authentication" in name or "permission" in name or "unauthorized" in name:
        return f"{stage} failed due to API authentication/permission. Check provider keys and model access."
    if "ratelimit" in name or "rate_limit" in name:
        return f"{stage} hit a rate limit. Retry shortly or lower request frequency."
    if "timeout" in name:
        return f"{stage} timed out. Retry with fewer rounds or try another model."
    return f"{stage} failed with `{type(exc).__name__}`: {exc}"

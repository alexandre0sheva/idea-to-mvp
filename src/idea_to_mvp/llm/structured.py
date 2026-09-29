"""Structured (schema-validated) model output with one feedback retry and an optional fallback."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, TypeVar

from langchain_core.exceptions import OutputParserException
from langchain_core.messages import BaseMessage, HumanMessage
from pydantic import BaseModel, ValidationError

from idea_to_mvp.llm.invoke import invoke_kwargs
from idea_to_mvp.llm.runtime import LlmRuntime

LOGGER = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_MAX_ATTEMPTS = 2


class StructuredOutputError(RuntimeError):
    """The model never produced output that satisfies the schema, and no fallback was given."""


def _structured_kwargs(runtime: LlmRuntime) -> dict[str, Any]:
    # Anthropic's native structured outputs; the default forced tool use is rejected by newer
    # Claude models (Sonnet 5.5, Opus 5.5, Fable 5.1).
    if runtime.provider == "anthropic":
        return {"method": "json_schema"}
    return {}


def invoke_structured(
    runtime: LlmRuntime,
    messages: list[BaseMessage],
    schema: type[T],
    *,
    fallback: Callable[[], T] | None = None,
) -> T:
    """Invoke the model and return a validated `schema` instance.

    On a parse/validation failure the call is retried once with the error appended to the
    conversation. If it still fails, `fallback()` supplies a deterministic default (logged as a
    warning); without a fallback a `StructuredOutputError` is raised.
    """
    structured = runtime.llm.with_structured_output(schema, **_structured_kwargs(runtime))
    attempt_messages = list(messages)
    last_error: Exception | None = None
    for _attempt in range(_MAX_ATTEMPTS):
        try:
            result = structured.invoke(attempt_messages, **invoke_kwargs(runtime, runtime.max_tokens))
            if result is None:
                raise ValueError("the model returned no structured output")
            return result if isinstance(result, schema) else schema.model_validate(result)
        except (ValidationError, OutputParserException, ValueError) as exc:
            last_error = exc
            LOGGER.warning("[%s] %s output was invalid: %s", runtime.provider, schema.__name__, exc)
            attempt_messages = [
                *messages,
                HumanMessage(
                    content=(
                        f"Your previous output was invalid: {exc}\n"
                        "Return output that satisfies the required schema exactly."
                    )
                ),
            ]
    if fallback is not None:
        LOGGER.warning("Using the deterministic fallback for %s after invalid model output.", schema.__name__)
        return fallback()
    raise StructuredOutputError(f"{schema.__name__}: model output stayed invalid: {last_error}") from last_error

"""Provider-agnostic text invocation: one call site that owns truncation and empty-output retries."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from idea_to_mvp.llm.runtime import LlmRuntime
from idea_to_mvp.schemas import http_url
from idea_to_mvp.text_utils import normalize_content

LOGGER = logging.getLogger(__name__)

EMPTY_OUTPUT_RETRY_INSTRUCTION = (
    "Your previous reply contained no visible text. Reply now with the final answer only, as plain "
    "Markdown, with no hidden reasoning."
)


def extract_text(message: AIMessage) -> str:
    content_text = normalize_content(message.content).strip()
    if content_text:
        return content_text
    # Some providers place text outside `content`.
    extra = getattr(message, "additional_kwargs", {}) or {}
    for key in ("text", "output_text", "completion"):
        val = extra.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    # Some OpenAI responses carry blocks in `refusal` even when content is empty.
    refusal_val = extra.get("refusal")
    if isinstance(refusal_val, str) and refusal_val.strip():
        return refusal_val.strip()
    if isinstance(refusal_val, list):
        bits: list[str] = []
        for item in refusal_val:
            if isinstance(item, str) and item.strip():
                bits.append(item.strip())
            elif isinstance(item, dict):
                txt = item.get("text")
                if isinstance(txt, str) and txt.strip():
                    bits.append(txt.strip())
        if bits:
            return "\n".join(bits)
    return ""


def invoke_kwargs(runtime: LlmRuntime, max_tokens: int) -> dict[str, Any]:
    """Per-request provider kwargs (Google needs the output cap on every call)."""
    if runtime.provider == "google":
        # LangChain's Google wrapper can ignore init-time generation kwargs, so pass the output
        # cap on each request to make Gemini honor the configured limit.
        return {"max_output_tokens": max_tokens}
    return {}


def _finish_reason(message: AIMessage) -> str:
    metadata = getattr(message, "response_metadata", None) or {}
    for key in ("finish_reason", "finishReason"):
        value = metadata.get(key)
        if value is not None:
            return str(value).strip().upper()
    return ""


def _looks_complete(text: str) -> bool:
    stripped = text.strip()
    return bool(stripped) and stripped.endswith((".", "!", "?", ")", "]", "}", "`", '"'))


def invoke_text(
    runtime: LlmRuntime,
    messages: list[BaseMessage],
    *,
    max_tokens: int | None = None,
) -> str:
    """Invoke the model and return its visible text ('' if it produced none).

    Retries at most twice, each only when needed: (1) Google output truncated by MAX_TOKENS gets a
    larger budget; (2) an empty reply is retried once with an instruction to answer visibly.
    """
    requested = max_tokens or runtime.max_tokens
    response = runtime.llm.invoke(messages, **invoke_kwargs(runtime, requested))
    text = extract_text(response)

    if runtime.provider == "google" and _finish_reason(response) == "MAX_TOKENS" and not _looks_complete(text):
        bigger = min(max(requested * 2, requested + 512), 8192)
        if bigger > requested:
            LOGGER.warning(
                "[Google] model=%s hit MAX_TOKENS with incomplete output; retrying with max_output_tokens=%s",
                runtime.model,
                bigger,
            )
            response = runtime.llm.invoke(messages, **invoke_kwargs(runtime, bigger))
            text = extract_text(response)

    if not text:
        LOGGER.warning("[%s] model=%s returned no visible text; retrying once.", runtime.provider, runtime.model)
        response = runtime.llm.invoke(
            [*messages, HumanMessage(content=EMPTY_OUTPUT_RETRY_INSTRUCTION)],
            **invoke_kwargs(runtime, requested),
        )
        text = extract_text(response)

    usage = getattr(response, "usage_metadata", None)
    LOGGER.debug("[%s] model=%s chars=%d usage=%s", runtime.provider, runtime.model, len(text), usage)
    return text


@dataclass(frozen=True)
class GroundedReply:
    """A web-grounded answer: its text and the URLs the provider says it cited."""

    text: str
    sources: list[str]


def collect_citation_urls(message: AIMessage) -> list[str]:
    """The http(s) URLs a web-grounded reply cites, in order and without duplicates.

    Each provider reports them its own way: Anthropic puts `citations` on text blocks, OpenAI `annotations`,
    Gemini `grounding_chunks` in the response metadata. Read from the raw shapes so nothing depends on
    LangChain's provider translation.
    """
    found: dict[str, None] = {}

    def take(url: object) -> None:
        if clean := http_url(url):
            found.setdefault(clean)

    if isinstance(message.content, list):
        for block in message.content:
            if not isinstance(block, dict):
                continue
            for key in ("citations", "annotations"):
                for item in block.get(key) or []:
                    if isinstance(item, dict):
                        take(item.get("url"))
    grounding = (getattr(message, "response_metadata", None) or {}).get("grounding_metadata") or {}
    for chunk in grounding.get("grounding_chunks") or []:
        if isinstance(chunk, dict):
            take((chunk.get("web") or {}).get("uri"))
    return list(found)


def invoke_grounded(runtime: LlmRuntime, messages: list[BaseMessage]) -> GroundedReply:
    """One call on a runtime that has a web search tool bound (`llm.search_runtime`): the reply's text and the
    sources it cited. No retry-for-empty like `invoke_text`: a search answer that is empty is just "found
    nothing", which the caller handles."""
    response = runtime.llm.invoke(messages, **invoke_kwargs(runtime, runtime.max_tokens))
    return GroundedReply(extract_text(response), collect_citation_urls(response))

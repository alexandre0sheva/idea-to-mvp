"""Provider-specific model construction. The only place that knows about vendor SDK quirks."""

from __future__ import annotations

import logging
import re
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from idea_to_mvp.config import Provider, Settings

LOGGER = logging.getLogger(__name__)

# Current-generation models reject (Claude 4.7+/5.x, GPT-5.x reasoning) or discourage (Gemini 3.x)
# custom sampling parameters, so a global TEMPERATURE must not be forwarded to them.
NO_SAMPLING_PARAMS = re.compile(
    r"^(claude-(opus-(5|4-[78])|sonnet-5|fable|mythos)|gemini-3|gpt-5)", flags=re.IGNORECASE
)

# Substring/prefix markers that identify which vendor a model name belongs to.
_PROVIDER_MARKERS: tuple[tuple[str, Provider], ...] = (
    ("claude", "anthropic"),
    ("gemini", "google"),
    ("gpt-", "openai"),
)


def sampling_kwargs(model: str, temperature: float | None) -> dict[str, Any]:
    if temperature is None or NO_SAMPLING_PARAMS.match((model or "").strip()):
        return {}
    return {"temperature": temperature}


def api_key_kwargs(key: str | None) -> dict[str, Any]:
    # Omit the argument when unset so the provider SDK falls back to its own env lookup.
    return {"api_key": SecretStr(key)} if key else {}


def warn_on_provider_mismatch(provider: Provider, model: str) -> None:
    """Log (never rewrite) an obviously wrong provider/model pairing, e.g. `openai` + `claude-*`."""
    name = (model or "").strip().lower()
    for marker, owner in _PROVIDER_MARKERS:
        if marker in name and owner != provider:
            LOGGER.warning(
                "Model %r looks like a %s model but the configured provider is %r; "
                "fix the *_PROVIDER / *_MODEL settings.",
                model,
                owner,
                provider,
            )
            return


def build_llm(provider: Provider, model: str, max_tokens: int, settings: Settings) -> BaseChatModel:
    warn_on_provider_mismatch(provider, model)
    sampling = sampling_kwargs(model, settings.temperature)
    if provider == "openai":
        return ChatOpenAI(  # type: ignore[call-arg]  # field aliases: max_completion_tokens
            model=model,
            max_tokens=max_tokens,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
            **api_key_kwargs(settings.openai_api_key),
            **sampling,
        )
    if provider == "anthropic":
        return ChatAnthropic(  # type: ignore[call-arg]  # field aliases: model_name
            model=model,
            max_tokens=max_tokens,
            timeout=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
            **api_key_kwargs(settings.anthropic_api_key),
            **sampling,
        )
    return ChatGoogleGenerativeAI(
        model=model,
        google_api_key=settings.google_api_key,
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
        **sampling,
    )

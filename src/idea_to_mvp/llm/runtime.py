from __future__ import annotations

from dataclasses import dataclass, replace
from functools import cache
from typing import Any, cast

from langchain_core.language_models.chat_models import BaseChatModel

from idea_to_mvp.config import Provider, clear_settings_cache, get_settings
from idea_to_mvp.llm.providers import build_llm, web_search_tool
from idea_to_mvp.roles import ROLES, resolve_role_model


@dataclass(frozen=True)
class LlmRuntime:
    llm: BaseChatModel
    provider: Provider
    model: str
    max_tokens: int


@dataclass(frozen=True)
class AgentRuntime(LlmRuntime):
    system_prompt: str


@cache
def get_runtime(role_key: str) -> AgentRuntime:
    settings = get_settings()
    spec = ROLES[role_key]
    provider, model = resolve_role_model(settings, role_key)
    max_tokens: int = getattr(settings, spec.max_tokens_setting)
    chat_model: BaseChatModel
    if settings.demo_mode:
        from idea_to_mvp.demo.models import DemoChatModel

        model, chat_model = "demo", DemoChatModel(role=role_key)
    else:
        chat_model = build_llm(provider, model, max_tokens, settings)
    # The role rides along in every run's metadata, so a streamed token says whose it is (`ui.view.panel_token`).
    chat_model.metadata = {**(chat_model.metadata or {}), "role": role_key}
    return AgentRuntime(
        llm=chat_model, provider=provider, model=model, max_tokens=max_tokens, system_prompt=spec.system_prompt
    )


def search_runtime(runtime: LlmRuntime, max_searches: int) -> LlmRuntime:
    """`runtime` with its provider's web search tool bound. Only the calls that need the web get this; structured
    calls keep the plain runtime (several providers cannot combine search with structured output)."""
    bound = runtime.llm.bind_tools([web_search_tool(runtime.provider, max_searches)])
    return replace(runtime, llm=cast(Any, bound))  # a bound model is a Runnable that invokes like the model


def clear_runtime_caches() -> None:
    clear_settings_cache()
    get_runtime.cache_clear()

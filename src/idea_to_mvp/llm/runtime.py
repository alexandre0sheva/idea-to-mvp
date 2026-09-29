from __future__ import annotations

from dataclasses import dataclass
from functools import cache

from langchain_core.language_models.chat_models import BaseChatModel

from idea_to_mvp.config import Provider, clear_settings_cache, get_settings
from idea_to_mvp.llm.providers import build_llm
from idea_to_mvp.roles import ROLES


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
    provider: Provider = getattr(settings, spec.provider_setting)
    model: str = getattr(settings, spec.model_setting)
    max_tokens: int = getattr(settings, spec.max_tokens_setting)
    if settings.demo_mode:
        from idea_to_mvp.demo.models import DemoChatModel

        return AgentRuntime(
            llm=DemoChatModel(role=role_key),
            provider=provider,
            model="demo",
            max_tokens=max_tokens,
            system_prompt=spec.system_prompt,
        )
    return AgentRuntime(
        llm=build_llm(provider, model, max_tokens, settings),
        provider=provider,
        model=model,
        max_tokens=max_tokens,
        system_prompt=spec.system_prompt,
    )


def clear_runtime_caches() -> None:
    clear_settings_cache()
    get_runtime.cache_clear()

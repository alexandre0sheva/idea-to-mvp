from __future__ import annotations

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import BaseModel

from idea_to_mvp.demo.fixtures import respond, respond_structured
from idea_to_mvp.text_utils import normalize_content


class DemoChatModel(BaseChatModel):
    """A chat model that answers from `fixtures.RESPONSES[role]` instead of calling an API."""

    role: str

    @property
    def _llm_type(self) -> str:
        return "idea-to-mvp-demo"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        text = respond(self.role, messages)
        # Rough token estimates so usage tracking and the UI badge work in demo mode too.
        prompt_chars = sum(len(normalize_content(m.content)) for m in messages)
        input_tokens, output_tokens = max(1, prompt_chars // 4), max(1, len(text) // 4)
        message = AIMessage(
            content=text,
            usage_metadata={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
            response_metadata={"model_name": "demo"},
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Runnable[Any, Any]:
        """Return the fixture instance for `schema` (a pydantic class) instead of calling a model."""
        if not (isinstance(schema, type) and issubclass(schema, BaseModel)):
            raise TypeError("DemoChatModel only supports pydantic schemas")

        def build(messages: Any, **_ignored: Any) -> BaseModel:
            return respond_structured(schema, list(messages))

        return RunnableLambda(build)

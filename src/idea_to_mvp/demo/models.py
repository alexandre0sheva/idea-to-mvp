from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from pydantic import BaseModel

from idea_to_mvp.demo.fixtures import respond, respond_structured
from idea_to_mvp.text_utils import normalize_content

_WORDS_PER_CHUNK = 4  # how coarsely a demo answer streams


def _pieces(text: str) -> list[str]:
    """`text` cut into a few words per piece, losing nothing (joined, the pieces are the text)."""
    words = re.findall(r"\S+\s*|\s+", text)
    return ["".join(words[i : i + _WORDS_PER_CHUNK]) for i in range(0, len(words), _WORDS_PER_CHUNK)]


class DemoChatModel(BaseChatModel):
    """A chat model that answers from `fixtures.RESPONSES[role]` instead of calling an API."""

    role: str

    @property
    def _llm_type(self) -> str:
        return "idea-to-mvp-demo"

    def _reply(self, messages: list[BaseMessage]) -> tuple[str, dict[str, Any]]:
        """The canned answer for this role and rough token estimates (so usage tracking and the UI badge
        work in demo mode too)."""
        text = respond(self.role, messages)
        prompt_chars = sum(len(normalize_content(m.content)) for m in messages)
        input_tokens, output_tokens = max(1, prompt_chars // 4), max(1, len(text) // 4)
        usage = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }
        return text, usage

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        text, usage = self._reply(messages)
        message = AIMessage(content=text, usage_metadata=usage, response_metadata={"model_name": "demo"})
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        """The same answer a few words at a time, so the panel streams in demo mode like a real model."""
        text, usage = self._reply(messages)
        for piece in _pieces(text):
            chunk = ChatGenerationChunk(message=AIMessageChunk(content=piece))
            if run_manager:
                run_manager.on_llm_new_token(piece, chunk=chunk)
            yield chunk
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content="",
                usage_metadata=usage,  # type: ignore[arg-type]
                response_metadata={"model_name": "demo"},
                chunk_position="last",
            )
        )

    def bind_tools(self, tools: Any, **kwargs: Any) -> Runnable[Any, Any]:
        """The demo model answers without tools (it has nothing to search), so binding the web search tool of
        the research step changes nothing: demo runs stay offline."""
        return self

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Runnable[Any, Any]:
        """Return the fixture instance for `schema` (a pydantic class) instead of calling a model."""
        if not (isinstance(schema, type) and issubclass(schema, BaseModel)):
            raise TypeError("DemoChatModel only supports pydantic schemas")

        def build(messages: Any, **_ignored: Any) -> BaseModel:
            return respond_structured(schema, list(messages))

        return RunnableLambda(build)

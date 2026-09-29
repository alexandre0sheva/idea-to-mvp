import logging
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, ValidationError

from idea_to_mvp.llm.structured import StructuredOutputError, invoke_structured


class Pet(BaseModel):
    name: str
    legs: int


class _Structured:
    def __init__(self, results: list, calls: list) -> None:
        self._results = results
        self._calls = calls

    def invoke(self, messages, **kwargs):
        self._calls.append((list(messages), kwargs))
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeLlm:
    def __init__(self, results: list) -> None:
        self.results = list(results)
        self.calls: list = []
        self.structured_kwargs: dict = {}
        self.schema = None

    def with_structured_output(self, schema, **kwargs):
        self.schema = schema
        self.structured_kwargs = kwargs
        return _Structured(self.results, self.calls)


def _runtime(llm: FakeLlm, provider: str = "openai"):
    return SimpleNamespace(llm=llm, provider=provider, model="m", max_tokens=100, system_prompt="s")


def _invalid() -> ValidationError:
    try:
        Pet.model_validate({"name": "x"})
    except ValidationError as exc:
        return exc
    raise AssertionError


MESSAGES = [HumanMessage(content="describe a pet")]


def test_returns_the_parsed_instance() -> None:
    llm = FakeLlm([Pet(name="Rex", legs=4)])
    assert invoke_structured(_runtime(llm), MESSAGES, Pet) == Pet(name="Rex", legs=4)
    assert llm.schema is Pet and len(llm.calls) == 1


def test_dict_results_are_validated_into_the_schema() -> None:
    llm = FakeLlm([{"name": "Tweety", "legs": 2}])
    assert invoke_structured(_runtime(llm), MESSAGES, Pet).legs == 2


def test_retries_once_with_the_validation_error_then_succeeds() -> None:
    llm = FakeLlm([_invalid(), Pet(name="Rex", legs=4)])
    assert invoke_structured(_runtime(llm), MESSAGES, Pet).name == "Rex"
    assert len(llm.calls) == 2
    feedback = str(llm.calls[1][0][-1].content)
    assert "invalid" in feedback.lower() and "legs" in feedback
    assert llm.calls[1][0][0].content == "describe a pet"  # original conversation preserved


def test_uses_fallback_after_two_failures_and_logs_a_warning(caplog) -> None:
    llm = FakeLlm([_invalid(), _invalid()])
    with caplog.at_level(logging.WARNING):
        result = invoke_structured(_runtime(llm), MESSAGES, Pet, fallback=lambda: Pet(name="Default", legs=0))
    assert result.name == "Default"
    assert len(llm.calls) == 2
    assert "fallback" in caplog.text.lower()


def test_raises_without_a_fallback() -> None:
    llm = FakeLlm([_invalid(), _invalid()])
    with pytest.raises(StructuredOutputError):
        invoke_structured(_runtime(llm), MESSAGES, Pet)


def test_none_result_counts_as_a_failure() -> None:
    llm = FakeLlm([None, Pet(name="Rex", legs=4)])
    assert invoke_structured(_runtime(llm), MESSAGES, Pet).name == "Rex"
    assert len(llm.calls) == 2


def test_anthropic_uses_native_json_schema_instead_of_forced_tool_use() -> None:
    llm = FakeLlm([Pet(name="Rex", legs=4)])
    invoke_structured(_runtime(llm, provider="anthropic"), MESSAGES, Pet)
    assert llm.structured_kwargs == {"method": "json_schema"}
    other = FakeLlm([Pet(name="Rex", legs=4)])
    invoke_structured(_runtime(other, provider="openai"), MESSAGES, Pet)
    assert other.structured_kwargs == {}


def test_google_gets_its_per_request_token_cap() -> None:
    llm = FakeLlm([Pet(name="Rex", legs=4)])
    invoke_structured(_runtime(llm, provider="google"), MESSAGES, Pet)
    assert llm.calls[0][1] == {"max_output_tokens": 100}

"""Every pipeline schema must be accepted by each provider's structured-output binding.

No network: the bindings are built with dummy keys; this catches schema shapes a provider's
strict mode would reject at bind time.
"""

import pytest
from langchain_anthropic import ChatAnthropic
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI

from idea_to_mvp.schemas import ArchitectureProposal, ExecutionStrategy, QuestionSet

SCHEMAS = [QuestionSet, ArchitectureProposal, ExecutionStrategy]


@pytest.mark.parametrize("schema", SCHEMAS)
def test_openai_accepts_schema(schema) -> None:
    llm = ChatOpenAI(model="gpt-5.6-terra", api_key="x")  # type: ignore[arg-type]
    assert llm.with_structured_output(schema) is not None


@pytest.mark.parametrize("schema", SCHEMAS)
def test_anthropic_accepts_schema_with_native_json_schema(schema) -> None:
    llm = ChatAnthropic(model="claude-sonnet-5-5", api_key="x")  # type: ignore[call-arg,arg-type]
    assert llm.with_structured_output(schema, method="json_schema") is not None


@pytest.mark.parametrize("schema", SCHEMAS)
def test_google_accepts_schema(schema) -> None:
    llm = ChatGoogleGenerativeAI(model="gemini-3.8-flash", google_api_key="x")
    assert llm.with_structured_output(schema) is not None

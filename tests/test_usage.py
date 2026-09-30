from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from idea_to_mvp import llm
from idea_to_mvp.state import IdeaDiscussionState
from idea_to_mvp.usage import UsageRecord, summarize_usage, with_usage


class UsageModel(BaseChatModel):
    """Reports fixed token usage per call, like a real provider would."""

    model_label: str = "fake-model"

    @property
    def _llm_type(self) -> str:
        return "usage-fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        message = AIMessage(
            content="ok",
            usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            response_metadata={"model_name": self.model_label},
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


class _Runtime:
    provider = "anthropic"
    model = "fake-model"


@pytest.fixture()
def fake_runtime(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(llm, "get_runtime", lambda role: _Runtime())


def test_with_usage_adds_one_record_per_model_summed_over_calls(fake_runtime) -> None:
    model = UsageModel()

    @with_usage(role="pm")
    def node(state: dict) -> dict:
        model.invoke("first")
        model.invoke("second")
        return {"turn_count": 1}

    update = node({})
    assert update["turn_count"] == 1
    assert update["usage"] == [
        {
            "role": "pm",
            "provider": "anthropic",
            "model": "fake-model",
            "input_tokens": 20,
            "output_tokens": 10,
            "cost_usd": None,
        }
    ]


def test_with_usage_leaves_updates_alone_when_no_model_was_called(fake_runtime) -> None:
    @with_usage(role="pm")
    def node(state: dict) -> dict:
        return {"x": 1}

    assert node({}) == {"x": 1}


def test_role_can_be_resolved_from_state(fake_runtime) -> None:
    model = UsageModel()
    seen: list[str] = []

    def role_of(state: dict) -> str:
        seen.append(state["speaker"])
        return state["speaker"]

    @with_usage(role=role_of)
    def node(state: dict) -> dict:
        model.invoke("x")
        return {}

    assert node({"speaker": "skeptic"})["usage"][0]["role"] == "skeptic"
    assert seen == ["skeptic"]


def test_with_usage_survives_a_runtime_lookup_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(role: str):
        raise RuntimeError("no keys")

    monkeypatch.setattr(llm, "get_runtime", broken)
    model = UsageModel()

    @with_usage(role="pm")
    def node(state: dict) -> dict:
        model.invoke("x")
        return {}

    assert node({})["usage"][0]["provider"] == ""


def test_with_usage_preserves_the_node_signature_for_langgraph() -> None:
    import inspect

    @with_usage(role="pm")
    def node(state: IdeaDiscussionState) -> dict:
        return {}

    assert list(inspect.signature(node).parameters) == ["state"]


def _record(role: str, model: str, i: int, o: int, cost: float | None = None) -> UsageRecord:
    return {"role": role, "provider": "p", "model": model, "input_tokens": i, "output_tokens": o, "cost_usd": cost}


def test_summarize_usage_totals_and_groups_by_role() -> None:
    summary = summarize_usage(
        [_record("pm", "a", 100, 50), _record("pm", "a", 10, 5), _record("architect", "b", 200, 100, 0.5)]
    )
    assert summary["input_tokens"] == 310 and summary["output_tokens"] == 155
    assert summary["total_tokens"] == 465 and summary["calls"] == 3
    assert summary["cost_usd"] == 0.5
    assert summary["by_role"]["pm"] == {"input_tokens": 110, "output_tokens": 55, "calls": 2, "cost_usd": None}
    assert summary["by_role"]["architect"]["cost_usd"] == 0.5


def test_summarize_usage_of_nothing_is_zero_and_has_no_cost() -> None:
    summary = summarize_usage([])
    assert summary["calls"] == 0 and summary["total_tokens"] == 0 and summary["cost_usd"] is None


def test_usage_is_captured_through_chains_like_structured_output(fake_runtime) -> None:
    from langchain_core.output_parsers import StrOutputParser

    chain = UsageModel() | StrOutputParser()  # same shape as `llm.with_structured_output(...)`

    @with_usage(role="architect")
    def node(state: dict) -> dict:
        chain.invoke("x")
        return {}

    assert node({})["usage"][0]["input_tokens"] == 10

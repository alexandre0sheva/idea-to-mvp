from typing import TypedDict

import pytest
from langgraph.graph import END, START, StateGraph

from idea_to_mvp.graph import run_config
from idea_to_mvp.resilience import LLM_RETRY, is_transient


class RateLimitError(Exception):
    pass


class APITimeoutError(Exception):
    pass


class APIConnectionError(Exception):
    pass


class AuthenticationError(Exception):
    pass


class PermissionDeniedError(Exception):
    pass


class HttpError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"http {status}")
        self.status_code = status


@pytest.mark.parametrize(
    "exc",
    [
        RateLimitError("429"),
        APITimeoutError("slow"),
        APIConnectionError("reset"),
        TimeoutError("t"),
        ConnectionResetError("c"),
        HttpError(429),
        HttpError(500),
        HttpError(503),
        HttpError(408),
    ],
)
def test_transient_errors_are_retryable(exc: Exception) -> None:
    assert is_transient(exc)


@pytest.mark.parametrize(
    "exc",
    [
        AuthenticationError("bad key"),
        PermissionDeniedError("no access"),
        HttpError(400),
        HttpError(401),
        HttpError(404),
        ValueError("bad output"),
        KeyError("x"),
    ],
)
def test_permanent_errors_are_not_retried(exc: Exception) -> None:
    assert not is_transient(exc)


class _S(TypedDict):
    calls: int


FAST_RETRY = LLM_RETRY._replace(initial_interval=0.0, jitter=False)


def _graph_with(node, policy=FAST_RETRY):
    builder = StateGraph(_S)
    builder.add_node("flaky", node, retry_policy=policy)
    builder.add_edge(START, "flaky")
    builder.add_edge("flaky", END)
    return builder.compile()


def test_node_is_retried_on_transient_errors_until_it_succeeds() -> None:
    attempts: list[int] = []

    def flaky(state: _S) -> dict:
        attempts.append(1)
        if len(attempts) < 3:
            raise RateLimitError("slow down")
        return {"calls": len(attempts)}

    assert _graph_with(flaky).invoke({"calls": 0}) == {"calls": 3}


def test_node_gives_up_after_the_maximum_attempts() -> None:
    attempts: list[int] = []

    def always(state: _S) -> dict:
        attempts.append(1)
        raise APITimeoutError("still down")

    with pytest.raises(APITimeoutError):
        _graph_with(always).invoke({"calls": 0})
    assert len(attempts) == LLM_RETRY.max_attempts == 3


def test_permanent_errors_fail_immediately() -> None:
    attempts: list[int] = []

    def bad_key(state: _S) -> dict:
        attempts.append(1)
        raise AuthenticationError("invalid api key")

    with pytest.raises(AuthenticationError):
        _graph_with(bad_key).invoke({"calls": 0})
    assert len(attempts) == 1


def test_llm_nodes_carry_the_retry_policy_and_gates_do_not() -> None:
    from idea_to_mvp.graph import build_graph

    nodes = build_graph().builder.nodes
    for name in ("summarizer", "architect", "strategy"):
        assert nodes[name].retry_policy, name
    for name in ("panel", "plan_bundle", "collect_answers", "arch_choice", "plan_gate", "implement_gate", "delivery_report"):
        assert not nodes[name].retry_policy, name


def test_run_config_names_and_tags_the_run_for_tracing() -> None:
    config = run_config("thread-42")
    assert config["configurable"] == {"thread_id": "thread-42"}
    assert config["run_name"] == "idea-to-mvp"
    assert "thread:thread-42" in config["tags"]

from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage

from idea_to_mvp.llm.invoke import extract_text, invoke_text


class ScriptedLlm:
    def __init__(self, replies: list[AIMessage]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[list, dict]] = []

    def invoke(self, messages, **kwargs):
        self.calls.append((list(messages), kwargs))
        return self.replies.pop(0)


def _runtime(llm: ScriptedLlm, provider: str = "anthropic"):
    return SimpleNamespace(llm=llm, provider=provider, model="m", max_tokens=100, system_prompt="s")


def test_returns_text_with_a_single_call() -> None:
    llm = ScriptedLlm([AIMessage(content="hello")])
    assert invoke_text(_runtime(llm), [HumanMessage(content="hi")]) == "hello"
    assert len(llm.calls) == 1


def test_empty_output_retries_once_with_visible_answer_instruction() -> None:
    llm = ScriptedLlm([AIMessage(content=""), AIMessage(content="second try")])
    text = invoke_text(_runtime(llm), [HumanMessage(content="hi")])
    assert text == "second try"
    assert len(llm.calls) == 2
    retry_messages = llm.calls[1][0]
    assert "visible" in str(retry_messages[-1].content).lower()
    assert retry_messages[0].content == "hi"  # original conversation is preserved


def test_empty_twice_returns_empty_string_after_exactly_one_retry() -> None:
    llm = ScriptedLlm([AIMessage(content=""), AIMessage(content="   ")])
    assert invoke_text(_runtime(llm), [HumanMessage(content="hi")]) == ""
    assert len(llm.calls) == 2


def test_google_truncated_output_retries_with_doubled_token_budget() -> None:
    truncated = AIMessage(content="cut off mid sentence", response_metadata={"finish_reason": "MAX_TOKENS"})
    llm = ScriptedLlm([truncated, AIMessage(content="A complete answer.")])
    text = invoke_text(_runtime(llm, provider="google"), [HumanMessage(content="hi")])
    assert text == "A complete answer."
    assert llm.calls[0][1] == {"max_output_tokens": 100}
    assert llm.calls[1][1] == {"max_output_tokens": 612}


def test_google_complete_output_at_max_tokens_is_not_retried() -> None:
    done = AIMessage(content="Finished.", response_metadata={"finish_reason": "MAX_TOKENS"})
    llm = ScriptedLlm([done])
    assert invoke_text(_runtime(llm, provider="google"), [HumanMessage(content="hi")]) == "Finished."
    assert len(llm.calls) == 1


def test_non_google_truncation_is_not_retried() -> None:
    truncated = AIMessage(content="cut off", response_metadata={"finish_reason": "MAX_TOKENS"})
    llm = ScriptedLlm([truncated])
    assert invoke_text(_runtime(llm), [HumanMessage(content="hi")]) == "cut off"
    assert len(llm.calls) == 1


def test_extract_text_handles_content_blocks_and_refusal() -> None:
    blocks = AIMessage(content=[{"type": "text", "text": "part one"}, {"type": "text", "text": "part two"}])
    assert extract_text(blocks) == "part one\npart two"
    refused = AIMessage(content="", additional_kwargs={"refusal": "I cannot help with that."})
    assert extract_text(refused) == "I cannot help with that."

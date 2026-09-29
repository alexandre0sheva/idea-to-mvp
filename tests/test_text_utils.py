from langchain_core.messages import HumanMessage

from idea_to_mvp.text_utils import normalize_content, normalize_message_content


def test_normalize_content_handles_mixed_list() -> None:
    content = ["alpha", {"text": "beta"}, {"not_text": "ignored"}]
    assert normalize_content(content) == "alpha\nbeta"


def test_normalize_message_content_handles_none() -> None:
    assert normalize_message_content(None) == ""


def test_normalize_message_content_handles_message() -> None:
    msg = HumanMessage(content=[{"text": "hello"}])
    assert normalize_message_content(msg) == "hello"

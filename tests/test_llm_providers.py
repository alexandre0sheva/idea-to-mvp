import pytest

from idea_to_mvp.llm.providers import sampling_kwargs, warn_on_provider_mismatch


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "claude-opus-4-8",
        "claude-fable-5-1",
        "gemini-3.8-flash",
        "gpt-5.6-terra",
    ],
)
def test_sampling_params_omitted_for_models_that_reject_them(model: str) -> None:
    assert sampling_kwargs(model, 0.7) == {}


@pytest.mark.parametrize("model", ["claude-haiku-4-5", "claude-sonnet-4-6", "gpt-4o-mini"])
def test_sampling_params_kept_for_models_that_accept_them(model: str) -> None:
    assert sampling_kwargs(model, 0.7) == {"temperature": 0.7}


def test_sampling_params_omitted_when_temperature_unset() -> None:
    assert sampling_kwargs("claude-haiku-4-5", None) == {}


def test_provider_mismatch_only_warns_and_never_rewrites(caplog) -> None:
    with caplog.at_level("WARNING"):
        warn_on_provider_mismatch("openai", "claude-sonnet-5-5")
    assert "claude-sonnet-5-5" in caplog.text
    caplog.clear()
    with caplog.at_level("WARNING"):
        warn_on_provider_mismatch("anthropic", "claude-sonnet-5-5")
        warn_on_provider_mismatch("openai", "opus-custom-model")  # no marker: silent
    assert caplog.text == ""


def test_every_provider_gets_the_configured_timeout_and_retries() -> None:
    from idea_to_mvp.config import Settings
    from idea_to_mvp.llm.providers import build_llm

    settings = Settings(_env_file=None, llm_timeout_seconds=45, llm_max_retries=4, openai_api_key="x",
                        anthropic_api_key="x", google_api_key="x")
    openai = build_llm("openai", "gpt-5.6-terra", 100, settings)
    anthropic = build_llm("anthropic", "claude-sonnet-5-5", 100, settings)
    google = build_llm("google", "gemini-3.8-flash", 100, settings)
    assert (openai.request_timeout, openai.max_retries) == (45, 4)
    assert (anthropic.default_request_timeout, anthropic.max_retries) == (45, 4)
    assert (google.timeout, google.max_retries) == (45, 4)

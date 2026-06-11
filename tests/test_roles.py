from config import Settings
from roles import DISCUSSION_ROLE_KEYS, ROLES, SPEAKER_NAME_TOKEN, SPEAKER_ORDER, TOKEN_TO_SPEAKER


def test_every_role_references_real_settings_fields() -> None:
    settings = Settings()
    for spec in ROLES.values():
        assert isinstance(getattr(settings, spec.provider_setting), str), spec.key
        assert isinstance(getattr(settings, spec.model_setting), str), spec.key
        assert isinstance(getattr(settings, spec.max_tokens_setting), int), spec.key
        assert spec.system_prompt.strip(), spec.key
        assert spec.display_name.strip(), spec.key


def test_expected_roles_present() -> None:
    assert set(ROLES) == {"pm", "tech_lead", "skeptic", "summarizer", "architect", "strategy", "planner"}


def test_speaker_order_derives_from_registry() -> None:
    assert SPEAKER_ORDER == ["PM", "Tech Lead", "Skeptic"]
    assert list(DISCUSSION_ROLE_KEYS) == ["pm", "tech_lead", "skeptic"]
    assert SPEAKER_NAME_TOKEN == {"PM": "pm", "Tech Lead": "tech_lead", "Skeptic": "skeptic"}
    assert TOKEN_TO_SPEAKER == {"pm": "PM", "tech_lead": "Tech Lead", "skeptic": "Skeptic"}


def test_skeptic_prompt_requires_constructive_pairing() -> None:
    assert "cheapest test" in ROLES["skeptic"].system_prompt

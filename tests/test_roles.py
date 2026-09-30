from idea_to_mvp.config import Settings
from idea_to_mvp.roles import (
    DISCUSSION_ROLE_KEYS,
    ROLES,
    SPEAKER_NAME_TOKEN,
    SPEAKER_ORDER,
    TOKEN_TO_SPEAKER,
)


def test_every_role_references_real_settings_fields() -> None:
    settings = Settings()
    for spec in ROLES.values():
        assert isinstance(getattr(settings, spec.provider_setting), str), spec.key
        assert isinstance(getattr(settings, spec.model_setting), str), spec.key
        assert isinstance(getattr(settings, spec.max_tokens_setting), int), spec.key
        assert spec.system_prompt.strip(), spec.key
        assert spec.display_name.strip(), spec.key


def test_expected_roles_present() -> None:
    assert set(ROLES) == {"pm", "tech_lead", "skeptic", "moderator", "summarizer", "architect", "strategy", "plan_writer", "change_planner", "blueprint_critic"}


def test_speaker_order_derives_from_registry() -> None:
    assert SPEAKER_ORDER == ["PM", "Tech Lead", "Skeptic"]
    assert list(DISCUSSION_ROLE_KEYS) == ["pm", "tech_lead", "skeptic"]
    assert SPEAKER_NAME_TOKEN == {"PM": "pm", "Tech Lead": "tech_lead", "Skeptic": "skeptic"}
    assert TOKEN_TO_SPEAKER == {"pm": "PM", "tech_lead": "Tech Lead", "skeptic": "Skeptic"}


def test_the_moderator_is_not_a_panelist() -> None:
    assert "moderator" not in DISCUSSION_ROLE_KEYS
    assert "Moderator" not in SPEAKER_ORDER
    prompt = ROLES["moderator"].system_prompt
    assert "converged" in prompt and "next_speaker" in prompt


def test_the_strategy_prompt_describes_agent_team_as_parallel_task_execution() -> None:
    prompt = ROLES["strategy"].system_prompt
    assert '"subagents"' in prompt and '"agent_team"' in prompt
    assert "parallel" in prompt and "git worktree" in prompt


def test_skeptic_prompt_requires_constructive_pairing() -> None:
    assert "cheapest test" in ROLES["skeptic"].system_prompt


def test_blueprint_roles_reuse_the_architect_model_with_their_own_token_budget() -> None:
    assert ROLES["plan_writer"].model_setting == ROLES["blueprint_critic"].model_setting == "architect_model"
    assert ROLES["plan_writer"].max_tokens_setting == "plan_max_tokens"
    planner = ROLES["plan_writer"].system_prompt
    assert "T01" in planner and "P0" in planner and "upstream PRD.md" in planner
    assert "blocker" in ROLES["blueprint_critic"].system_prompt and "warning" in ROLES["blueprint_critic"].system_prompt

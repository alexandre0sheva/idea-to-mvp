from pathlib import Path

from dotenv import dotenv_values

from idea_to_mvp.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


def test_env_example_matches_settings_defaults(monkeypatch) -> None:
    """Every KEY=value in .env.example must equal the corresponding Settings default."""
    values = {k: v for k, v in dotenv_values(ENV_EXAMPLE).items() if v not in (None, "")}
    for key in values:
        monkeypatch.delenv(key, raising=False)
    defaults = Settings(_env_file=None)
    checked = 0
    for key, raw in values.items():
        field = key.lower()
        if field.endswith("_api_key") or field not in Settings.model_fields:
            continue
        expected = getattr(defaults, field)
        if isinstance(expected, bool):
            actual: object = raw.lower() == "true"
        elif isinstance(expected, Path):
            actual = Path(raw).expanduser()
        else:
            actual = type(expected)(raw)
        assert actual == expected, f"{key}: .env.example={raw!r} but Settings default={expected!r}"
        checked += 1
    assert checked >= 15


def test_every_model_setting_is_documented_in_env_example() -> None:
    documented = {k.lower() for k in dotenv_values(ENV_EXAMPLE)}
    for field in Settings.model_fields:
        if field.endswith("_model") or field.endswith("_provider"):
            assert field in documented, f"{field.upper()} missing from .env.example"

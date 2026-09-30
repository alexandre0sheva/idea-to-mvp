from pathlib import Path

import pytest

from idea_to_mvp.config import Settings, clear_settings_cache, get_settings
from idea_to_mvp.exporter import save_session_export
from idea_to_mvp.ui.view import ViewEntry


def test_output_dirs_derive_from_output_dir(tmp_path: Path) -> None:
    settings = Settings(output_dir=tmp_path)
    assert settings.exports_dir == tmp_path / "exports"
    assert settings.blueprints_dir == tmp_path / "blueprints"
    assert settings.projects_dir == tmp_path / "projects"


def test_default_output_dir_is_outside_the_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OUTPUT_DIR", raising=False)
    default = Settings(_env_file=None).output_dir
    assert default == Path("~/idea-to-mvp").expanduser()
    repo_root = Path(__file__).resolve().parents[1]
    assert repo_root not in default.parents and default != repo_root


def test_output_dir_expands_user_home() -> None:
    assert Settings(output_dir="~/somewhere").output_dir == Path.home() / "somewhere"


def test_export_is_written_under_exports_dir(tmp_path: Path) -> None:
    path, _json = save_session_export(
        exports_dir=tmp_path / "exports",
        entries=[ViewEntry("idea", "You", "An idea")],
        thread_id="t1",
        mode="idea",
    )
    assert path.parent == tmp_path / "exports"
    assert path.read_text(encoding="utf-8").startswith("# Idea-to-MVP Session Export")


def test_cached_settings_pick_up_output_dir_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    clear_settings_cache()
    try:
        assert get_settings().blueprints_dir == tmp_path / "blueprints"
        assert get_settings().projects_dir == tmp_path / "projects"
    finally:
        clear_settings_cache()

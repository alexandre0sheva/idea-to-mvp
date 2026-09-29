from idea_to_mvp.config import Settings
from idea_to_mvp.ui.app import make_ui


def test_make_ui_smoke() -> None:
    demo = make_ui(settings=Settings())
    assert demo is not None


def _markdown_values(demo) -> list[str]:
    return [str(getattr(b, "value", "")) for b in demo.blocks.values() if type(b).__name__ == "Markdown"]


def test_demo_banner_only_shown_in_demo_mode() -> None:
    assert any("Demo mode" in v for v in _markdown_values(make_ui(settings=Settings(demo_mode=True))))
    assert not any("Demo mode" in v for v in _markdown_values(make_ui(settings=Settings(demo_mode=False))))

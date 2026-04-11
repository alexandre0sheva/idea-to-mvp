from app import make_ui
from config import Settings


def test_make_ui_smoke() -> None:
    demo = make_ui(settings=Settings())
    assert demo is not None

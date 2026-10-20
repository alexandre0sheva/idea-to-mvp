import pytest

from app import __version__
from app.cli import main


def test_main_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "ready" in capsys.readouterr().out


def test_version_flag_prints_the_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stop:
        main(["--version"])
    assert stop.value.code == 0
    assert __version__ in capsys.readouterr().out

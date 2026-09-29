from __future__ import annotations

from pathlib import Path

import pytest

from idea_to_mvp.config import clear_settings_cache
from idea_to_mvp.llm import clear_runtime_caches


@pytest.fixture()
def demo_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Offline demo mode with every output (and the checkpoint DB) under `tmp_path`."""
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    monkeypatch.delenv("CHECKPOINT_DB", raising=False)
    monkeypatch.setenv("CHECKPOINTER", "sqlite")
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    clear_settings_cache()
    clear_runtime_caches()
    yield tmp_path
    clear_runtime_caches()
    clear_settings_cache()


@pytest.fixture()
async def demo_service(demo_env: Path):
    """(SubmitService, GraphProvider) on a real SQLite checkpointer in demo mode."""
    from idea_to_mvp.config import get_settings
    from idea_to_mvp.graph import GraphProvider
    from idea_to_mvp.sessions import SessionRegistry
    from idea_to_mvp.ui.service import AppContext, SubmitService

    settings = get_settings()
    graphs = GraphProvider(settings)
    registry = SessionRegistry(settings.checkpoint_path)
    yield SubmitService(AppContext(settings=settings, graphs=graphs, registry=registry)), graphs
    await graphs.aclose()

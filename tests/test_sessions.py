from datetime import UTC, datetime, timedelta
from pathlib import Path

from idea_to_mvp.sessions import SessionRegistry


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.now += timedelta(minutes=1)
        return self.now


def test_registry_lists_newest_first_and_upsert_updates_in_place(tmp_path: Path) -> None:
    registry = SessionRegistry(tmp_path / "nested" / "sessions.db", clock=Clock())
    registry.upsert("t1", "First idea", "discussion")
    registry.upsert("t2", "Second idea", "summary")
    registry.upsert("t1", "First idea", "answers", interrupted_at="answers")

    sessions = registry.list()
    assert [s.thread_id for s in sessions] == ["t1", "t2"]
    first = sessions[0]
    assert first.stage == "answers" and first.interrupted_at == "answers"
    assert first.created_at < first.updated_at  # created_at survives the update
    assert sessions[1].interrupted_at is None


def test_registry_delete_and_limit(tmp_path: Path) -> None:
    registry = SessionRegistry(tmp_path / "sessions.db", clock=Clock())
    for index in range(5):
        registry.upsert(f"t{index}", f"Idea {index}", "discussion")
    assert len(registry.list(limit=3)) == 3
    registry.delete("t4")
    registry.delete("does-not-exist")
    assert "t4" not in [s.thread_id for s in registry.list()]


def test_registry_is_created_lazily(tmp_path: Path) -> None:
    db = tmp_path / "deep" / "sessions.db"
    registry = SessionRegistry(db)
    assert not db.parent.exists()
    assert registry.list() == []
    assert db.exists()

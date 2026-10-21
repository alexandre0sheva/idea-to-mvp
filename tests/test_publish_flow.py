"""Publishing through the real graph (demo mode): the record lands in the thread's state, the gate survives it, and
the CLI, the iterate gate payload, and the dashboard all agree."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from typing import Any

import pytest
from test_publish import TOKEN, FakeGitHub, git

from idea_to_mvp import cli, publish
from idea_to_mvp.config import get_settings
from idea_to_mvp.graph import open_graph, read_thread, update_thread
from idea_to_mvp.headless import HeadlessOptions, run_idea
from idea_to_mvp.sessions import SessionRegistry
from idea_to_mvp.ui.dashboard import render_dashboard

RECORD = {
    "repo": "alex/habit-tracker",
    "url": "https://github.com/alex/habit-tracker",
    "visibility": "private",
    "branch": "main",
    "commit": "abc1234",
    "tags": ["v0.1"],
    "files": 12,
    "backend": "token",
    "published_at": "2026-10-20T12:00:00",
}


@pytest.fixture()
def cwd(demo_env: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(demo_env)  # never pick up the developer's real .env
    monkeypatch.setattr(
        publish.shutil, "which", lambda name: None
    )  # whatever the developer has installed
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_OWNER", "PUBLISH_VISIBILITY"):
        monkeypatch.delenv(name, raising=False)
    from idea_to_mvp.config import clear_settings_cache

    clear_settings_cache()
    return demo_env


async def delivered(settings: Any) -> str:
    """A demo run built and verified, waiting at the iterate gate: its thread id."""
    async with open_graph(settings) as graph:
        summary = await run_idea(
            graph,
            SessionRegistry(settings.checkpoint_path),
            "a habit tracker",
            HeadlessOptions(autopilot=True, implement=True),
            settings=settings,
            progress=lambda line: None,
        )
    return summary.thread_id


async def view(settings: Any, thread: str) -> Any:
    async with open_graph(settings) as graph:
        return await read_thread(graph, thread)


# ------------------------------------------------------------------- state


async def test_the_iterate_gate_tells_the_ui_what_publishing_offers(
    cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    from idea_to_mvp.config import clear_settings_cache

    clear_settings_cache()
    settings = get_settings()
    thread = await delivered(settings)
    gate = (await view(settings, thread)).interrupt
    assert gate is not None and gate["kind"] == "iterate_gate"
    assert gate["publish"]["available"] is True and gate["publish"]["backend"] == "token"
    assert gate["publish"]["visibility"] == "private" and gate["publish"]["published"] == {}
    assert gate["publish"]["repo"].endswith("habit-tracker")


async def test_recording_a_publication_keeps_the_gate_pending_and_shows_it_in_the_payload(
    cwd: Path,
) -> None:
    settings = get_settings()
    thread = await delivered(settings)
    async with open_graph(settings) as graph:
        before = await read_thread(graph, thread)
        assert before.interrupt is not None and before.values["publication"] == {}
        await update_thread(graph, thread, {"publication": RECORD})
        after = await read_thread(graph, thread)
    assert after.interrupt is not None and after.interrupt["kind"] == "iterate_gate"
    assert after.next_nodes == ("iterate_gate",)
    assert after.values["publication"] == RECORD
    assert (
        after.interrupt["publish"]["published"] == RECORD
    )  # the gate's payload was rebuilt from the new state
    assert after.values["workspace_dir"] == before.values["workspace_dir"]


async def test_recording_after_a_reflection_does_not_reflect_again(
    cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the experience library on, `reflect` is the last node before the gate: the update must not re-run it."""
    monkeypatch.setenv("EXPERIENCE_LIBRARY", "on")
    from idea_to_mvp.config import clear_settings_cache

    clear_settings_cache()
    settings = get_settings()
    thread = await delivered(settings)
    async with open_graph(settings) as graph:
        before = await read_thread(graph, thread)
        assert before.values["reflection"]  # the reflection ran before the gate
        entries = sorted(path.name for path in settings.library_dir.rglob("*") if path.is_file())
        await update_thread(graph, thread, {"publication": RECORD})
        after = await read_thread(graph, thread)
    assert (
        after.interrupt is not None
        and after.interrupt["kind"] == "iterate_gate"
        and after.next_nodes == ("iterate_gate",)
    )
    assert after.values["reflection"] == before.values["reflection"] and len(
        after.values["usage"]
    ) == len(before.values["usage"])
    assert (
        sorted(path.name for path in settings.library_dir.rglob("*") if path.is_file()) == entries
    )


async def test_a_finished_session_can_record_a_publication_too(cwd: Path) -> None:
    settings = get_settings()
    thread = await delivered(settings)
    from langgraph.types import Command

    from idea_to_mvp.graph import run_config

    async with open_graph(settings) as graph:
        await graph.ainvoke(Command(resume={"iterate": False, "feedback": ""}), run_config(thread))
        assert (await read_thread(graph, thread)).next_nodes == ()
        await update_thread(graph, thread, {"publication": RECORD})
        done = await read_thread(graph, thread)
    assert done.next_nodes == () and done.interrupt is None and done.values["publication"] == RECORD


async def test_a_publish_that_cannot_be_noted_in_the_session_still_reports_the_repository(
    cwd: Path, hub: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    from idea_to_mvp import headless

    settings = get_settings()
    thread = await delivered(settings)

    async def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("checkpoint database is locked")

    monkeypatch.setattr(headless, "update_thread", broken)
    async with open_graph(settings) as graph:
        values = (await read_thread(graph, thread)).values
        plan = await asyncio.to_thread(publish.plan_publish, values, settings, hub)
        record, unrecorded = await headless.publish_and_record(
            graph, thread, plan, hub, confirm=plan.fingerprint
        )
    assert (
        record["url"].startswith("https://github.com/alex/")
        and "locked" in unrecorded
        and len(hub.created) == 1
    )


# --------------------------------------------------------------- dashboard


def dashboard(**fields: Any) -> str:
    return render_dashboard(
        {"verification": {"passed": True, "lanes": []}, "iteration": 1, **fields}
    )


def test_the_dashboard_links_the_published_repository() -> None:
    html = dashboard(publication=RECORD)
    assert (
        "Published" in html
        and "href='https://github.com/alex/habit-tracker'" in html
        and "private" in html
    )
    assert "v0.1" in html and "abc1234" in html


def test_the_dashboard_says_nothing_of_publishing_before_it_happened() -> None:
    assert "Published" not in dashboard()


def test_the_dashboard_escapes_the_record_and_links_only_https() -> None:
    hostile = {**RECORD, "repo": "<img src=x onerror=alert(1)>", "url": "javascript:alert(1)"}
    html = dashboard(publication=hostile)
    assert "<img" not in html and "&lt;img" in html
    assert "<a " not in html and "href=" not in html


# --------------------------------------------------------------------- cli


@pytest.fixture()
def hub(cwd: Path, monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    (cwd / "remote").mkdir()
    fake = FakeGitHub(cwd / "remote")
    monkeypatch.setattr(publish, "make_client", lambda settings, **kwargs: fake)
    return fake


def run_publish(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = cli.main(["publish", *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_a_dry_run_shows_the_plan_and_creates_nothing(
    cwd: Path, hub: FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    thread = asyncio.run(delivered(get_settings()))
    code, out, _err = run_publish(capsys, thread, "--dry-run")
    assert code == 0 and "alex/" in out and "private" in out and "main" in out and "v0.1" in out
    assert (
        hub.created == []
        and (asyncio.run(view(get_settings(), thread))).values["publication"] == {}
    )


def test_json_output_of_a_dry_run_is_the_plan(
    cwd: Path, hub: FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    thread = asyncio.run(delivered(get_settings()))
    code, out, _err = run_publish(
        capsys, thread, "--dry-run", "--json", "--repo", "acme/tracker", "--visibility", "public"
    )
    plan = json.loads(out)
    assert (
        code == 0
        and plan["ok"] is True
        and plan["full_name"] == "acme/tracker"
        and plan["visibility"] == "public"
    )
    assert (
        plan["tags"] == ["v0.1"]
        and plan["fingerprint"]
        and ".idea-to-mvp" not in " ".join(plan["files"])
    )


def test_declining_at_the_prompt_publishes_nothing(
    cwd: Path, hub: FakeGitHub, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    thread = asyncio.run(delivered(get_settings()))
    stdin = io.StringIO("n\n")
    stdin.isatty = lambda: True  # type: ignore[method-assign]
    monkeypatch.setattr("sys.stdin", stdin)
    code, out, err = run_publish(capsys, thread)
    assert code == 3 and "Nothing was pushed" in err and out == ""
    assert (
        hub.created == []
        and (asyncio.run(view(get_settings(), thread))).values["publication"] == {}
    )


def test_without_a_terminal_publishing_needs_yes(
    cwd: Path, hub: FakeGitHub, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    thread = asyncio.run(delivered(get_settings()))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # not a terminal
    code, _out, err = run_publish(capsys, thread)
    assert code == 1 and "--yes" in err and hub.created == []


def test_confirming_publishes_pushes_and_records_the_repository_in_the_session(
    cwd: Path, hub: FakeGitHub, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    thread = asyncio.run(delivered(get_settings()))
    stdin = io.StringIO("yes\n")
    stdin.isatty = lambda: True  # type: ignore[method-assign]
    monkeypatch.setattr("sys.stdin", stdin)
    code, out, err = run_publish(capsys, thread, "--repo", "tracker")
    assert code == 0 and "https://github.com/alex/tracker" in out
    assert "alex/tracker" in err  # the preview went to stderr, the result to stdout
    assert hub.created[0]["private"] is True
    bare = hub.bare("tracker")
    assert git(bare, "tag", "--list") == "v0.1" and git(bare, "rev-parse", "--verify", "main")
    assert ".idea-to-mvp" not in git(bare, "ls-tree", "-r", "--name-only", "main")
    after = asyncio.run(view(get_settings(), thread))
    assert after.values["publication"]["url"] == "https://github.com/alex/tracker"
    assert (
        after.interrupt is not None and after.interrupt["kind"] == "iterate_gate"
    )  # the session can still iterate

    code, _out, err = run_publish(
        capsys, thread, "--yes"
    )  # a second time: already published, nothing created
    assert code == 1 and "already published" in err and len(hub.created) == 1


def test_yes_confirms_the_plan_it_printed(
    cwd: Path, hub: FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    thread = asyncio.run(delivered(get_settings()))
    code, out, _err = run_publish(capsys, thread, "--yes", "--json", "--visibility", "public")
    record = json.loads(out)
    assert code == 0 and record["visibility"] == "public" and hub.created[0]["private"] is False
    assert TOKEN not in out


def test_publishing_without_github_access_explains_how(
    cwd: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    thread = asyncio.run(delivered(get_settings()))
    monkeypatch.setattr(publish, "make_client", lambda settings, **kwargs: None)
    code, _out, err = run_publish(capsys, thread, "--yes")
    assert code == 1 and "GITHUB_TOKEN" in err


def test_an_unknown_session_is_an_error(
    cwd: Path, hub: FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _out, err = run_publish(capsys, "no-such-session", "--dry-run")
    assert code == 1 and "no-such-session" in err


def test_the_publish_flags_are_validated(capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (["publish"], ["publish", "t", "--visibility", "secret"]):
        with pytest.raises(SystemExit) as usage:
            cli.main(argv)
        assert usage.value.code == 1  # a usage error exits 1, like every command
        capsys.readouterr()

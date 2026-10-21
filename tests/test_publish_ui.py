"""Publish to GitHub in the UI: the iterate gate's two side actions (review, then confirm) on the real graph."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
from service_helpers import gate_field, mode_of, panel_visible, submit, update_value
from test_publish import FakeGitHub, git

from idea_to_mvp import publish
from idea_to_mvp.graph import read_thread
from idea_to_mvp.publish import Repo
from idea_to_mvp.ui.gates import GATES, GateRuntime, publish_info_html, publish_preview_html
from idea_to_mvp.ui.service import SubmitService
from idea_to_mvp.ui.view import (
    ARCH_CHOICE_A,
    IMPL_CHOICE_START,
    MODE_ITERATE_GATE,
    PLAN_CHOICE_GENERATE,
)


@pytest.fixture(autouse=True)
def no_github_in_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        publish.shutil, "which", lambda name: None
    )  # whatever the developer has installed
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_OWNER", "PUBLISH_VISIBILITY"):
        monkeypatch.delenv(name, raising=False)


async def at_the_iterate_gate(
    demo_service: Any, thread: str, *, access: bool = True
) -> tuple[SubmitService, FakeGitHub, Any]:
    service, graphs = demo_service
    fake = FakeGitHub(service._context.settings.output_dir)
    service = SubmitService(
        dataclasses.replace(service._context, github=lambda settings: fake if access else None)
    )
    await submit(service, "A habit tracker for climbing gyms", "", thread)
    await submit(service, "1. Solo climbers.", "", thread)
    await submit(service, "", ARCH_CHOICE_A, thread)
    await submit(service, "", PLAN_CHOICE_GENERATE, thread)
    await submit(service, "", IMPL_CHOICE_START, thread)
    assert await mode_of(service, thread) == MODE_ITERATE_GATE
    return service, fake, await graphs.get()


async def test_reviewing_shows_the_plan_and_creates_nothing(demo_service: Any) -> None:
    service, fake, _graph = await at_the_iterate_gate(demo_service, "pub-review")
    screen = await service.publish_review("pub-review", "", "private")
    assert (
        screen.confirm is True and screen.fingerprint and "Nothing exists on GitHub" in screen.info
    )
    assert "alex/" in screen.preview and "private" in screen.preview and "v0.1" in screen.preview
    assert ".idea-to-mvp/" in screen.preview and "Never sent" in screen.preview
    assert fake.created == []


async def test_confirming_without_a_review_creates_nothing_and_asks_for_one(
    demo_service: Any,
) -> None:
    service, fake, _graph = await at_the_iterate_gate(demo_service, "pub-noreview")
    for shown in ("", None, "0123456789abcdef"):
        screen = await service.publish_confirm("pub-noreview", "", "private", shown)
        assert (
            screen.confirm is True
            and screen.fingerprint
            and "not the one you reviewed" in screen.info
        )
    assert fake.created == []


async def test_a_plan_changed_after_the_review_must_be_reviewed_again(demo_service: Any) -> None:
    service, fake, _graph = await at_the_iterate_gate(demo_service, "pub-changed")
    reviewed = await service.publish_review("pub-changed", "", "private")
    screen = await service.publish_confirm(
        "pub-changed", "", "public", reviewed.fingerprint
    )  # visibility edited since
    assert (
        "not the one you reviewed" in screen.info
        and "public" in screen.preview
        and fake.created == []
    )
    assert screen.fingerprint != reviewed.fingerprint


async def test_the_confirmation_reuses_the_client_of_the_review(demo_service: Any) -> None:
    service, fake, _graph = await at_the_iterate_gate(demo_service, "pub-client")
    made: list[Any] = []
    service._context = dataclasses.replace(
        service._context, github=lambda settings: made.append(1) or fake
    )
    reviewed = await service.publish_review("pub-client", "", "private")
    await service.publish_confirm("pub-client", "", "private", reviewed.fingerprint)
    assert len(made) == 1  # one client, so one account lookup for the review and the confirmation


async def test_a_second_confirm_while_one_is_running_is_refused_and_nothing_else_moves(
    demo_service: Any,
) -> None:
    import asyncio

    service, fake, graph = await at_the_iterate_gate(demo_service, "pub-twice")
    reviewed = await service.publish_review("pub-twice", "", "private")
    entered, release = asyncio.Event(), asyncio.Event()
    real_create = fake.create_repo

    def slow_create(*args: Any, **kwargs: Any) -> Repo:
        entered.set()
        return real_create(*args, **kwargs)

    fake.create_repo = slow_create  # type: ignore[method-assign]
    original = service._publish_plan

    async def held(*args: Any, **kwargs: Any) -> Any:
        result = await original(*args, **kwargs)
        await release.wait()  # the first confirm is parked here, holding the slot
        return result

    service._publish_plan = held  # type: ignore[method-assign]
    first = asyncio.create_task(
        service.publish_confirm("pub-twice", "", "private", reviewed.fingerprint)
    )
    while "pub-twice" not in service._publishing:
        await asyncio.sleep(0)
    second = await service.publish_confirm("pub-twice", "", "private", reviewed.fingerprint)
    review = await service.publish_review("pub-twice", "", "private")
    assert "already in progress" in second.info and "already in progress" in review.info
    outputs = [
        o async for o in service.handle_submit("", 1, "pub-twice", gate_inputs={"action": "finish"})
    ]
    assert "in progress" in str(
        update_value(outputs[-1][0], "value")
    )  # a decision cannot move the graph meanwhile
    assert (await read_thread(graph, "pub-twice")).interrupt is not None
    release.set()
    done = await first
    assert "https://github.com/alex/" in done.info and len(fake.created) == 1
    assert "pub-twice" not in service._publishing


async def test_confirming_the_reviewed_plan_publishes_records_it_and_keeps_the_gate(
    demo_service: Any,
) -> None:
    service, fake, graph = await at_the_iterate_gate(demo_service, "pub-go")
    reviewed = await service.publish_review("pub-go", "tracker", "private")
    done = await service.publish_confirm("pub-go", "tracker", "private", reviewed.fingerprint)

    assert (
        done.confirm is False
        and done.preview == ""
        and "https://github.com/alex/tracker" in done.info
    )
    assert fake.created == [
        {
            "owner": "alex",
            "name": "tracker",
            "private": True,
            "description": "Built with idea-to-mvp",
        }
    ]
    bare = fake.bare("tracker")
    assert git(bare, "tag", "--list") == "v0.1" and ".idea-to-mvp" not in git(
        bare, "ls-tree", "-r", "--name-only", "main"
    )
    view = await read_thread(graph, "pub-go")
    assert view.values["publication"]["url"] == "https://github.com/alex/tracker"
    assert (
        view.interrupt is not None and view.interrupt["kind"] == "iterate_gate"
    )  # still waiting for feedback
    assert view.interrupt["publish"]["published"]["repo"] == "alex/tracker"

    # the next render of the gate says so, locks the fields, and a second attempt is refused
    values, interrupt, _next = await service._read("pub-go")
    updates = GATES["iterate_gate"].render(interrupt).updates
    assert (
        "Published" in updates["publish_info"]["value"]
        and updates["publish_repo"]["interactive"] is False
    )
    again = await service.publish_review("pub-go", "other", "private")
    assert again.confirm is False and "already published" in again.preview
    assert len(fake.created) == 1


async def test_a_failure_is_shown_and_nothing_is_recorded(
    demo_service: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, fake, graph = await at_the_iterate_gate(demo_service, "pub-fail")

    def taken(*args: Any, **kwargs: Any) -> Repo:
        from idea_to_mvp.publish import PublishError

        raise PublishError("GitHub: a repository with that name already exists")

    monkeypatch.setattr(fake, "create_repo", taken)
    reviewed = await service.publish_review("pub-fail", "", "private")
    failed = await service.publish_confirm("pub-fail", "", "private", reviewed.fingerprint)
    assert "already exists" in failed.info and failed.confirm is False and failed.fingerprint == ""
    assert (await read_thread(graph, "pub-fail")).values["publication"] == {}


async def test_publishing_without_github_access_says_what_is_needed(demo_service: Any) -> None:
    service, _fake, _graph = await at_the_iterate_gate(demo_service, "pub-noaccess", access=False)
    screen = await service.publish_review("pub-noaccess", "", "private")
    assert screen.confirm is False and "GITHUB_TOKEN" in screen.preview


async def test_publishing_is_only_offered_at_the_iterate_gate(demo_service: Any) -> None:
    service, _graphs = demo_service
    for action in (
        service.publish_review("never-started", "", "private"),
        service.publish_confirm("never-started", "", "private", "x"),
    ):
        screen = await action
        assert screen.confirm is False and "iterate gate" in screen.info


async def test_publishing_waits_for_a_running_step(demo_service: Any) -> None:
    service, _fake, _graph = await at_the_iterate_gate(demo_service, "pub-busy")
    service._active.add("pub-busy")
    screen = await service.publish_review("pub-busy", "", "private")
    assert "still running" in screen.info and screen.confirm is False


async def test_the_iterate_form_shows_publishing_defaults_when_the_gate_renders(
    demo_service: Any,
) -> None:
    service, _fake, _graph = await at_the_iterate_gate(demo_service, "pub-form")
    outputs = await service._render("pub-form")
    assert panel_visible(outputs, "iterate_gate") is True
    info = update_value(gate_field(outputs, "iterate_gate", "publish_info"), "value")
    assert "GITHUB_TOKEN" in info  # no token in the test environment: it says what is needed
    assert update_value(gate_field(outputs, "iterate_gate", "publish_repo"), "value").endswith(
        "a-habit-tracker-for-climbing-gyms"
    )
    assert update_value(gate_field(outputs, "iterate_gate", "publish_confirm"), "visible") is False
    assert update_value(gate_field(outputs, "iterate_gate", "publish_preview"), "visible") is False


# ------------------------------------------------------------ html


def test_the_info_note_covers_each_situation() -> None:
    assert "GITHUB_TOKEN" in publish_info_html({"available": False})
    assert "GITHUB_TOKEN" in publish_info_html({"available": True, "backend": "token"})
    assert "gh CLI" in publish_info_html({"available": True, "backend": "gh"})
    done = publish_info_html(
        {"published": {"repo": "a/b", "url": "https://github.com/a/b", "visibility": "private"}}
    )
    assert "href='https://github.com/a/b'" in done
    hostile = publish_info_html({"published": {"repo": "<b>x</b>", "url": "javascript:alert(1)"}})
    assert "<b>" not in hostile and "href=" not in hostile


def test_the_preview_escapes_what_agents_wrote() -> None:
    from idea_to_mvp.publish import PublishPlan

    plan = PublishPlan(
        workspace="/w",
        owner="a",
        name="b",
        visibility="private",
        description="d",
        backend="token",
        branch="main",
        commit="a" * 40,
        tags=("v0.1",),
        commits=("abc <script>alert(1)</script>",),
        commit_count=500,
        files=tuple(f"dir/<f{i}>.py" for i in range(150)),
        problems=(),
    )
    html = publish_preview_html(plan)
    assert "<script>" not in html and "&lt;script&gt;" in html and "&lt;f0&gt;" in html
    assert (
        "and 50 more" in html
        and "150 file(s)" in html
        and "removed from the latest version" not in html
    )
    gone = publish_preview_html(dataclasses.replace(plan, removed=("old/<secret>.txt",)))
    assert (
        "1 more file(s) in the history" in gone
        and "&lt;secret&gt;" in gone
        and "<secret>" not in gone
    )
    assert "Cannot publish" in publish_preview_html(
        dataclasses.replace(plan, problems=("<x>",))
    ) and "&lt;x&gt;" in publish_preview_html(dataclasses.replace(plan, problems=("<x>",)))


def test_the_app_wires_the_publish_buttons(demo_env: Path) -> None:
    from idea_to_mvp.ui.app import make_ui

    app = make_ui()
    labels = {str(getattr(block, "value", None)) for block in app.blocks.values()}
    assert (
        "Review what will be pushed" in labels
        and "Confirm: create the repository and push" in labels
    )
    clicks = [
        fn
        for fn in app.fns.values()
        if getattr(fn.fn, "__name__", "") in ("on_review", "on_confirm")
    ]
    assert len(clicks) == 2


def test_a_gate_without_the_publish_actions_wires_nothing() -> None:
    GATES["iterate_gate"].wire({}, GateRuntime(thread=None, payload=None))  # type: ignore[arg-type]

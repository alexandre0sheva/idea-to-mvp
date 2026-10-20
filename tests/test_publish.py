"""Publishing a delivery to GitHub: the preview, the confirmation, and what is (and is never) pushed.

GitHub is faked entirely: a fake client "creates" a bare repository on disk, so the push is real git against it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from idea_to_mvp import publish
from idea_to_mvp.config import Settings, clear_settings_cache
from idea_to_mvp.delivery import tag_iteration
from idea_to_mvp.implementation.workspace import commit_workspace, prepare_workspace
from idea_to_mvp.publish import (
    GhClient,
    NotConfirmed,
    PublishError,
    Repo,
    RestClient,
    default_repo_name,
    make_client,
    plan_publish,
    publish_options,
    publish_plan,
    render_plan,
)

TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.chdir(tmp_path)  # Settings reads .env from the working directory: never the developer's own
    for name in ("GITHUB_TOKEN", "GITHUB_OWNER", "PUBLISH_VISIBILITY", "GH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    clear_settings_cache()
    yield
    clear_settings_cache()


def git(workspace: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=workspace, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """A delivered project: the blueprint and a second commit, tagged v0.1, with an orchestrator record beside it."""
    bundle = tmp_path / "blueprints" / "20260101-000000-000000-a-habit-tracker"
    bundle.mkdir(parents=True)
    (bundle / "README.md").write_text("# habit tracker")
    ws = prepare_workspace(bundle, tmp_path / "projects")
    (ws / "src").mkdir()
    (ws / "src" / "app.py").write_text("print('hi')\n")
    (ws / ".idea-to-mvp").mkdir()
    (ws / ".idea-to-mvp" / "ledger.json").write_text("{}")  # git-ignored: never committed
    (ws / ".env").write_text("OPENAI_API_KEY=sk-secret\n")  # git-ignored too
    commit_workspace(ws, "T01: app")
    tag_iteration(ws, 1)
    return ws


def state_of(workspace: Path, **fields: Any) -> dict[str, Any]:
    return {"workspace_dir": str(workspace), "delivery_report": "# Delivery report", "iteration": 1, **fields}


class FakeGitHub:
    """What the GitHub client does, with a bare repository on disk where the repository would be."""

    backend = "token"

    def __init__(self, root: Path, *, login: str = "alex", token: str = TOKEN, clone_url: str | None = None) -> None:
        self.root = root
        self._login = login
        self._token = token
        self._clone_url = clone_url
        self.created: list[dict[str, Any]] = []

    def login(self) -> str:
        return self._login

    def create_repo(self, owner: str, name: str, *, private: bool, description: str) -> Repo:
        self.created.append({"owner": owner, "name": name, "private": private, "description": description})
        bare = self.root / f"{owner}-{name}.git"
        subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
        return Repo(f"{owner}/{name}", f"https://github.com/{owner}/{name}", self._clone_url or str(bare))

    def push_token(self) -> str:
        return self._token

    def bare(self, name: str) -> Path:
        return self.root / f"{self._login}-{name}.git"


@pytest.fixture()
def hub(tmp_path: Path) -> FakeGitHub:
    (tmp_path / "remote").mkdir()
    return FakeGitHub(tmp_path / "remote")


def settings(**fields: Any) -> Settings:
    return Settings(_env_file=None, **fields)  # type: ignore[call-arg]


# ------------------------------------------------------------------- names


def test_the_repository_name_is_the_project_folder_without_its_timestamp() -> None:
    assert default_repo_name(Path("/p/20261020-232931-394718-a-habit-tracker")) == "a-habit-tracker"
    assert default_repo_name(Path("/p/20261020-232931-394718-Habit Tracker!! (v2)")) == "Habit-Tracker-v2"
    assert default_repo_name(Path("/p/20261020-232931-394718-")) == "project"
    assert len(default_repo_name(Path("/p/" + "x" * 300))) == 100


# -------------------------------------------------------------------- plans


def test_the_plan_names_the_repository_private_by_default_and_what_would_be_pushed(workspace: Path, hub: FakeGitHub) -> None:
    plan = plan_publish(state_of(workspace), settings(), hub)
    assert plan.problems == () and plan.ok
    assert plan.full_name == "alex/a-habit-tracker" and plan.visibility == "private"
    assert plan.branch == "main" and plan.tags == ("v0.1",) and plan.commit_count == 2
    assert plan.commit == git(workspace, "rev-parse", "HEAD")
    assert {"README.md", "src/app.py", ".gitignore"} <= set(plan.files)
    assert plan.refspecs == (f"{plan.commit}:refs/heads/main", "refs/tags/v0.1:refs/tags/v0.1")
    assert hub.created == []  # planning creates nothing


def test_orchestrator_records_and_secret_files_are_never_in_the_plan(workspace: Path, hub: FakeGitHub) -> None:
    plan = plan_publish(state_of(workspace), settings(), hub)
    assert not any(".idea-to-mvp" in path or path == ".env" for path in plan.files)
    text = render_plan(plan)
    assert ".idea-to-mvp/" in text and "never" in text.lower()  # the preview says what stays home


def test_the_owner_and_visibility_come_from_the_request_then_the_settings(workspace: Path, hub: FakeGitHub) -> None:
    configured = settings(github_owner="acme", publish_visibility="public")
    plan = plan_publish(state_of(workspace), configured, hub)
    assert (plan.full_name, plan.visibility) == ("acme/a-habit-tracker", "public")
    asked = plan_publish(state_of(workspace), configured, hub, repo="bob/tracker", visibility="private")
    assert (asked.full_name, asked.visibility) == ("bob/tracker", "private")
    assert plan_publish(state_of(workspace), settings(), hub, repo="tracker").full_name == "alex/tracker"


@pytest.mark.parametrize("repo", ["a b", "bad/name/extra", "-flag", "x" * 101, "o/", "../up", "name.git", "."])
def test_an_unusable_repository_name_is_a_problem(workspace: Path, hub: FakeGitHub, repo: str) -> None:
    plan = plan_publish(state_of(workspace), settings(), hub, repo=repo)
    assert not plan.ok and "repository name" in " ".join(plan.problems).lower()


def test_an_unknown_visibility_is_a_problem(workspace: Path, hub: FakeGitHub) -> None:
    assert not plan_publish(state_of(workspace), settings(), hub, visibility="internal").ok


def test_without_github_access_the_plan_says_how_to_get_it(workspace: Path) -> None:
    plan = plan_publish(state_of(workspace), settings(), None)
    assert not plan.ok and "GITHUB_TOKEN" in " ".join(plan.problems) and "gh" in " ".join(plan.problems)


def test_a_session_without_a_delivery_has_nothing_to_publish(workspace: Path, hub: FakeGitHub, tmp_path: Path) -> None:
    for state in (
        {},
        {"workspace_dir": str(tmp_path / "gone"), "delivery_report": "x"},
        state_of(workspace, delivery_report=""),
    ):
        plan = plan_publish(state, settings(), hub)
        assert not plan.ok and plan.problems
    plain = tmp_path / "plain"
    plain.mkdir()
    assert "git repository" in " ".join(plan_publish(state_of(plain), settings(), hub).problems)


def test_a_published_session_is_not_published_again(workspace: Path, hub: FakeGitHub) -> None:
    published = {"repo": "alex/x", "url": "https://github.com/alex/x"}
    plan = plan_publish(state_of(workspace, publication=published), settings(), hub)
    assert not plan.ok and "https://github.com/alex/x" in " ".join(plan.problems)


def test_a_failed_login_is_a_problem_not_a_crash(workspace: Path) -> None:
    class Rejected(FakeGitHub):
        def login(self) -> str:
            raise PublishError("GitHub rejected the token (401)")

    plan = plan_publish(state_of(workspace), settings(), Rejected(workspace.parent))
    assert not plan.ok and "rejected the token" in " ".join(plan.problems)


def test_history_that_ever_held_an_orchestrator_record_or_a_secret_is_refused(workspace: Path, hub: FakeGitHub) -> None:
    git(workspace, "add", "-f", ".idea-to-mvp/ledger.json", ".env")
    git(workspace, "-c", "user.name=a", "-c", "user.email=a@b", "commit", "-q", "-m", "oops")
    git(workspace, "rm", "-q", "--cached", ".idea-to-mvp/ledger.json", ".env")  # gone from the tip, still in history
    git(workspace, "-c", "user.name=a", "-c", "user.email=a@b", "commit", "-q", "-m", "undo")
    plan = plan_publish(state_of(workspace), settings(), hub)
    assert not plan.ok
    problem = " ".join(plan.problems)
    assert ".idea-to-mvp/ledger.json" in problem and ".env" in problem


def test_an_env_example_is_not_a_secret(workspace: Path, hub: FakeGitHub) -> None:
    (workspace / ".env.example").write_text("KEY=\n")
    (workspace / "sub").mkdir()
    (workspace / "sub" / ".env.local").write_text("KEY=x\n")
    commit_workspace(workspace, "T02: env")  # .env.* is git-ignored, .env.example is not
    plan = plan_publish(state_of(workspace), settings(), hub)
    assert plan.ok and ".env.example" in plan.files and "sub/.env.local" not in plan.files


def test_only_tags_of_a_version_on_the_branch_are_pushed(workspace: Path, hub: FakeGitHub) -> None:
    git(workspace, "tag", "release-candidate")
    git(workspace, "checkout", "-q", "-b", "side", "HEAD~1")
    (workspace / "other.txt").write_text("x")
    commit_workspace(workspace, "side work")
    git(workspace, "tag", "v0.9")  # on another branch: not part of what main is
    git(workspace, "checkout", "-q", "main")
    assert plan_publish(state_of(workspace), settings(), hub).tags == ("v0.1",)


def test_the_fingerprint_covers_everything_the_user_confirms(workspace: Path, hub: FakeGitHub) -> None:
    base = plan_publish(state_of(workspace), settings(), hub)
    again = plan_publish(state_of(workspace), settings(), hub)
    assert base.fingerprint == again.fingerprint
    assert plan_publish(state_of(workspace), settings(), hub, visibility="public").fingerprint != base.fingerprint
    assert plan_publish(state_of(workspace), settings(), hub, repo="other").fingerprint != base.fingerprint
    (workspace / "more.txt").write_text("x")
    commit_workspace(workspace, "T02: more")
    assert plan_publish(state_of(workspace), settings(), hub).fingerprint != base.fingerprint
    tag_iteration(workspace, 2)
    assert plan_publish(state_of(workspace), settings(), hub).tags == ("v0.1", "v0.2")


def test_the_rendered_plan_is_the_whole_preview(workspace: Path, hub: FakeGitHub) -> None:
    text = render_plan(plan_publish(state_of(workspace), settings(), hub))
    for expected in ("alex/a-habit-tracker", "private", "main", "v0.1", "src/app.py", "README.md"):
        assert expected in text
    refused = render_plan(plan_publish(state_of(workspace), settings(), None))
    assert "Cannot publish" in refused and "GITHUB_TOKEN" in refused


# --------------------------------------------------------------- publishing


def test_nothing_is_created_or_pushed_without_the_confirmation_of_this_exact_plan(workspace: Path, hub: FakeGitHub) -> None:
    plan = plan_publish(state_of(workspace), settings(), hub)
    for wrong in ("", "0" * 16, plan_publish(state_of(workspace), settings(), hub, visibility="public").fingerprint):
        with pytest.raises(NotConfirmed):
            publish_plan(plan, hub, confirm=wrong)
    assert hub.created == [] and list(hub.root.iterdir()) == []


def test_a_plan_with_problems_cannot_be_published_even_when_confirmed(workspace: Path, hub: FakeGitHub) -> None:
    plan = plan_publish(state_of(workspace), settings(), hub, repo="bad name")
    with pytest.raises(PublishError):
        publish_plan(plan, hub, confirm=plan.fingerprint)
    assert hub.created == []


def test_a_confirmed_plan_creates_a_private_repository_and_pushes_the_branch_and_the_version_tags(
    workspace: Path, hub: FakeGitHub
) -> None:
    git(workspace, "branch", "task/T01")  # work branches stay home
    plan = plan_publish(state_of(workspace), settings(), hub)
    record = publish_plan(plan, hub, confirm=plan.fingerprint)

    assert hub.created == [
        {"owner": "alex", "name": "a-habit-tracker", "private": True, "description": publish.DESCRIPTION}
    ]
    bare = hub.bare("a-habit-tracker")
    assert git(bare, "rev-parse", "refs/heads/main") == plan.commit
    assert git(bare, "tag", "--list") == "v0.1"
    assert git(bare, "branch", "--list").split() == ["main"]  # not task/T01
    tree = set(git(bare, "ls-tree", "-r", "--name-only", "main").splitlines())
    assert tree == set(plan.files)
    assert not any(".idea-to-mvp" in path or path == ".env" for path in tree)
    assert git(bare, "log", "--format=%s", "main").splitlines() == ["T01: app", "blueprint"]  # the delivery's own history

    assert record["repo"] == "alex/a-habit-tracker" and record["url"] == "https://github.com/alex/a-habit-tracker"
    assert record["visibility"] == "private" and record["branch"] == "main" and record["tags"] == ["v0.1"]
    assert record["commit"] == plan.commit and record["files"] == len(plan.files) and record["published_at"]
    assert TOKEN not in json.dumps(record)


def test_a_public_repository_is_created_only_when_public_was_confirmed(workspace: Path, hub: FakeGitHub) -> None:
    plan = plan_publish(state_of(workspace), settings(), hub, visibility="public")
    publish_plan(plan, hub, confirm=plan.fingerprint)
    assert hub.created[0]["private"] is False


def test_the_token_reaches_git_in_its_environment_scoped_to_github_never_in_the_arguments(
    workspace: Path, hub: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []
    real = publish.run_git

    def spy(path: Path, *args: str, env: Any = None, timeout: float = 120) -> Any:
        calls.append((args, dict(env or {})))
        return real(path, *args, env=env, timeout=timeout)

    monkeypatch.setattr(publish, "run_git", spy)
    plan = plan_publish(state_of(workspace), settings(), hub)
    publish_plan(plan, hub, confirm=plan.fingerprint)
    (args, env) = next(call for call in calls if call[0][:1] == ("push",))
    assert TOKEN not in " ".join(args) and "--force" not in args and "-f" not in args and "--mirror" not in args
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader" and env["GIT_CONFIG_COUNT"] == "1"
    import base64

    assert base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode() in env["GIT_CONFIG_VALUE_0"]


def test_a_push_that_fails_names_the_repository_it_left_behind_and_never_prints_the_token(
    workspace: Path, tmp_path: Path
) -> None:
    broken = FakeGitHub(tmp_path / "remote", clone_url=str(tmp_path / "does-not-exist" / "x.git"))
    (tmp_path / "remote").mkdir()
    plan = plan_publish(state_of(workspace), settings(), broken)
    with pytest.raises(PublishError) as failed:
        publish_plan(plan, broken, confirm=plan.fingerprint)
    message = str(failed.value)
    assert "https://github.com/alex/a-habit-tracker" in message and "push" in message.lower()
    assert TOKEN not in message


def test_a_repository_that_cannot_be_created_pushes_nothing(workspace: Path, hub: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    def taken(*args: Any, **kwargs: Any) -> Repo:
        raise PublishError("a repository named alex/a-habit-tracker already exists")

    monkeypatch.setattr(hub, "create_repo", taken)
    plan = plan_publish(state_of(workspace), settings(), hub)
    with pytest.raises(PublishError, match="already exists"):
        publish_plan(plan, hub, confirm=plan.fingerprint)
    assert list(hub.root.iterdir()) == []


def test_a_git_folder_swapped_for_a_link_after_the_plan_is_refused(workspace: Path, hub: FakeGitHub, tmp_path: Path) -> None:
    plan = plan_publish(state_of(workspace), settings(), hub)
    elsewhere = tmp_path / "elsewhere"
    (workspace / ".git").rename(elsewhere)
    (workspace / ".git").symlink_to(elsewhere)
    with pytest.raises(PublishError, match="not a plain directory"):
        publish_plan(plan, hub, confirm=plan.fingerprint)
    assert hub.created == []


def test_publishing_hardens_the_repository_before_git_touches_it(workspace: Path, hub: FakeGitHub) -> None:
    plan = plan_publish(state_of(workspace), settings(), hub)
    hook = workspace / ".git" / "hooks" / "pre-push"  # planted after the plan, before the push
    hook.parent.mkdir(exist_ok=True)
    marker = workspace / "hook-ran"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    with (workspace / ".git" / "config").open("a") as config:
        config.write(f"[core]\n\tfsmonitor = touch {marker}\n")
    publish_plan(plan, hub, confirm=plan.fingerprint)
    assert not marker.exists()


# ------------------------------------------------------------------ clients


class Recorder:
    """A fake HTTP layer: (method, url, headers, body) in, (status, json) out."""

    def __init__(self, *replies: tuple[int, dict[str, Any]]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, str, dict[str, str], dict[str, Any] | None]] = []

    def __call__(self, method: str, url: str, headers: dict[str, str], body: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
        self.calls.append((method, url, headers, body))
        return self.replies.pop(0)


REPO_JSON = {
    "full_name": "alex/tracker",
    "html_url": "https://github.com/alex/tracker",
    "clone_url": "https://github.com/alex/tracker.git",
}


def test_the_rest_client_creates_a_repository_in_the_users_account() -> None:
    send = Recorder((200, {"login": "alex"}), (201, REPO_JSON))
    client = RestClient(TOKEN, send=send)
    repo = client.create_repo("alex", "tracker", private=True, description="d")
    assert repo == Repo("alex/tracker", "https://github.com/alex/tracker", "https://github.com/alex/tracker.git")
    (login, create) = send.calls
    assert login[:2] == ("GET", "https://api.github.com/user")
    assert create[:2] == ("POST", "https://api.github.com/user/repos")
    assert create[3] == {"name": "tracker", "private": True, "description": "d", "auto_init": False}
    assert create[2]["Authorization"] == f"Bearer {TOKEN}" and "X-GitHub-Api-Version" in create[2]
    assert client.push_token() == TOKEN


def test_the_rest_client_creates_a_repository_in_an_organisation() -> None:
    send = Recorder((200, {"login": "alex"}), (201, REPO_JSON))
    RestClient(TOKEN, send=send).create_repo("acme", "tracker", private=False, description="d")
    assert send.calls[1][1] == "https://api.github.com/orgs/acme/repos" and send.calls[1][3]["private"] is False


def test_the_login_is_asked_once() -> None:
    send = Recorder((200, {"login": "alex"}), (201, REPO_JSON))
    client = RestClient(TOKEN, send=send)
    assert client.login() == "alex" and client.login() == "alex"
    client.create_repo("alex", "tracker", private=True, description="d")
    assert [call[1] for call in send.calls].count("https://api.github.com/user") == 1


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, {"message": "Bad credentials"}, "rejected the token"),
        (403, {"message": "Resource not accessible by personal access token"}, "not accessible"),
        (404, {"message": "Not Found"}, "not found"),
        (422, {"message": "Repository creation failed.", "errors": [{"message": "name already exists on this account"}]}, "already exists"),
        (500, {}, "500"),
    ],
)
def test_the_rest_client_turns_api_errors_into_readable_ones_without_the_token(status: int, body: dict, expected: str) -> None:
    client = RestClient(TOKEN, send=Recorder((200, {"login": "alex"}), (status, body)))
    with pytest.raises(PublishError) as failed:
        client.create_repo("alex", "tracker", private=True, description="d")
    assert expected in str(failed.value).lower() and TOKEN not in str(failed.value)


def test_a_clone_url_that_is_not_https_is_refused() -> None:
    odd = {**REPO_JSON, "clone_url": "ext::sh -c evil"}
    client = RestClient(TOKEN, send=Recorder((200, {"login": "alex"}), (201, odd)))
    with pytest.raises(PublishError, match="clone"):
        client.create_repo("alex", "tracker", private=True, description="d")


class FakeRun:
    def __init__(self, *outputs: tuple[int, str, str]) -> None:
        self.outputs = list(outputs)
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((command, kwargs.get("input")))
        code, out, err = self.outputs.pop(0)
        return subprocess.CompletedProcess(command, code, out, err)


def test_the_gh_client_uses_the_cli_for_the_api_and_for_the_token() -> None:
    run = FakeRun((0, json.dumps({"login": "alex"}), ""), (0, json.dumps(REPO_JSON), ""), (0, f"{TOKEN}\n", ""))
    client = GhClient(run=run, gh="/usr/bin/gh")
    assert client.login() == "alex"
    assert client.create_repo("acme", "tracker", private=True, description="d").url == "https://github.com/alex/tracker"
    assert client.push_token() == TOKEN
    commands = [call[0] for call in run.calls]
    assert commands[0][:3] == ["/usr/bin/gh", "api", "/user"]
    assert commands[1][:5] == ["/usr/bin/gh", "api", "--method", "POST", "/orgs/acme/repos"]
    assert json.loads(run.calls[1][1] or "")["name"] == "tracker"
    assert commands[2] == ["/usr/bin/gh", "auth", "token"]


def test_a_failing_gh_call_is_a_publish_error() -> None:
    client = GhClient(run=FakeRun((1, "", "gh: To get started with GitHub CLI, please run: gh auth login")), gh="gh")
    with pytest.raises(PublishError, match="gh auth login"):
        client.login()


def test_the_client_is_chosen_by_what_is_configured() -> None:
    with_token = make_client(settings(github_token=TOKEN), which=lambda name: "/usr/bin/gh")
    assert isinstance(with_token, RestClient)  # an explicit token wins over the CLI
    assert isinstance(make_client(settings(), which=lambda name: "/usr/bin/gh"), GhClient)
    assert make_client(settings(), which=lambda name: None) is None


def test_the_gate_offers_publishing_when_there_is_a_way_to_do_it(workspace: Path) -> None:
    on = publish_options(state_of(workspace), settings(github_token=TOKEN), which=lambda name: None)
    assert on == {
        "available": True,
        "backend": "token",
        "repo": "a-habit-tracker",
        "visibility": "private",
        "published": {},
    }
    off = publish_options(state_of(workspace), settings(), which=lambda name: None)
    assert off["available"] is False and off["backend"] == ""
    assert publish_options(state_of(workspace), settings(), which=lambda name: "/usr/bin/gh")["backend"] == "gh"
    published = publish_options(state_of(workspace, publication={"url": "https://github.com/a/b"}), settings(github_token=TOKEN))
    assert published["published"] == {"url": "https://github.com/a/b"}
    assert publish_options({}, settings(github_token=TOKEN))["available"] is False  # nothing delivered yet


# ------------------------------------------------------------ history, hosts, transport


def test_files_that_only_the_history_holds_are_listed_as_pushed_too(workspace: Path, hub: FakeGitHub) -> None:
    (workspace / "notes.txt").write_text("a key I pasted: not really a secret file name\n")
    commit_workspace(workspace, "T02: notes")
    git(workspace, "rm", "-q", "notes.txt")
    commit_workspace(workspace, "T03: drop notes")
    plan = plan_publish(state_of(workspace), settings(), hub)
    assert plan.ok and "notes.txt" not in plan.files and plan.removed == ("notes.txt",)
    assert "notes.txt" in render_plan(plan) and "pushed with it" in render_plan(plan)
    assert plan_publish(state_of(workspace), settings(), hub).to_dict()["removed"] == ("notes.txt",)
    pushed = publish_plan(plan, hub, confirm=plan.fingerprint)
    assert "notes.txt" in git(hub.bare("a-habit-tracker"), "log", "--name-only", "--format=", "main")  # it was sent
    assert pushed["files"] == len(plan.files)


@pytest.mark.parametrize(
    ("remote", "key"),
    [
        ("https://github.com/alex/x.git", "http.https://github.com/.extraheader"),
        ("https://ghe.example.com/alex/x.git", "http.https://ghe.example.com/.extraheader"),
        ("/tmp/local/bare.git", "http.https://github.com/.extraheader"),
    ],
)
def test_the_credential_is_scoped_to_the_host_of_the_remote_it_is_sent_to(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, remote: str, key: str
) -> None:
    seen: dict[str, str] = {}

    def spy(path: Path, *args: str, env: Any = None, timeout: float = 120) -> Any:
        seen.update(env or {})
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(publish, "run_git", spy)
    publish._push(workspace, remote, ("refs/heads/main:refs/heads/main",), TOKEN)
    assert seen["GIT_CONFIG_KEY_0"] == key


class FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def fake_urlopen(monkeypatch: pytest.MonkeyPatch, outcome: Any) -> list[Any]:
    requests: list[Any] = []

    def urlopen(request: Any, timeout: float = 0) -> Any:
        requests.append(request)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(publish.urllib.request, "urlopen", urlopen)
    return requests


def test_the_transport_sends_json_and_returns_the_status_and_body(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = fake_urlopen(monkeypatch, FakeResponse(201, b'{"full_name": "a/b"}'))
    status, body = publish._send("POST", "https://api.github.com/user/repos", {"Accept": "x"}, {"name": "b"})
    assert (status, body) == (201, {"full_name": "a/b"})
    assert requests[0].get_method() == "POST" and json.loads(requests[0].data) == {"name": "b"}


def test_an_http_error_keeps_its_status_and_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import urllib.error

    error = urllib.error.HTTPError("u", 422, "Unprocessable", {}, io.BytesIO(b'{"message": "already exists"}'))  # type: ignore[arg-type]
    fake_urlopen(monkeypatch, error)
    assert publish._send("POST", "https://api.github.com/x", {}, {}) == (422, {"message": "already exists"})
    error = urllib.error.HTTPError("u", 502, "Bad Gateway", {}, io.BytesIO(b"<html>oops</html>"))  # type: ignore[arg-type]
    fake_urlopen(monkeypatch, error)
    assert publish._send("GET", "https://api.github.com/x", {}, None) == (502, {})


def test_a_success_that_is_not_json_is_not_reported_as_a_network_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_urlopen(monkeypatch, FakeResponse(200, b"<html>proxy login</html>"))
    with pytest.raises(PublishError, match="not JSON"):
        publish._send("GET", "https://api.github.com/user", {}, None)


def test_an_unreachable_github_is_a_publish_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    fake_urlopen(monkeypatch, urllib.error.URLError("name resolution failed"))
    with pytest.raises(PublishError, match="could not reach GitHub: name resolution failed"):
        publish._send("GET", "https://api.github.com/user", {}, None)
    fake_urlopen(monkeypatch, TimeoutError("timed out"))
    with pytest.raises(PublishError, match="could not reach GitHub"):
        publish._send("GET", "https://api.github.com/user", {}, None)

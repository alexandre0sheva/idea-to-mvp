"""Publish a delivery to GitHub: opt-in, previewed, and confirmed.

Nothing here runs by itself. The user asks for it at the iterate gate or with `idea-to-mvp publish <thread>`;
`plan_publish` works out the repository, its visibility, and exactly what would be pushed (the branch and the
`v0.<n>` tags of the delivery's own git history, never `.idea-to-mvp/` or a secret file) and `publish_plan` creates
the repository and pushes only for a confirmation that carries that plan's fingerprint, so what the user saw is what
is sent. A plan that would push an orchestrator record or a secret anywhere in the history is refused.

The credential is `GITHUB_TOKEN` or the `gh` CLI's sign-in. It stays in the orchestrator: the agents' environment
is scrubbed of it (`implementation/options.py`), and it reaches git as a header in git's environment, scoped to
github.com, never in an argument list. A repository is only ever created (an existing name is an error), and the
push is never forced. LLM-free; the clients are small so tests fake them.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from idea_to_mvp.config import Settings
from idea_to_mvp.delivery import is_secret_env
from idea_to_mvp.implementation.workspace import harden_git_dir, run_git

LOGGER = logging.getLogger(__name__)

__all__ = [
    "DESCRIPTION",
    "GhClient",
    "GitHubClient",
    "NotConfirmed",
    "PublishError",
    "PublishPlan",
    "Repo",
    "RestClient",
    "default_repo_name",
    "make_client",
    "plan_publish",
    "publish_options",
    "publish_plan",
    "render_plan",
]

DESCRIPTION = "Built with idea-to-mvp"
VISIBILITIES = ("private", "public")
FILE_LIST_LIMIT = 100  # file names the preview lists; the plan keeps them all
HISTORY_LIMIT = 30  # commits the preview lists
NO_ACCESS = (
    "no GitHub access: set GITHUB_TOKEN in .env (a token that can create repositories) or sign in with "
    "`gh auth login`"
)
_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,99}$")
_OWNER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_VERSION_TAG = re.compile(r"^v0\.(\d+)$")
_FOLDER_PREFIX = re.compile(r"^\d{8}-\d{6}(?:-\d+)?-")
_API = "https://api.github.com"


class PublishError(RuntimeError):
    """Publishing failed; the message is for the user and never holds the token."""


class NotConfirmed(PublishError):
    """The confirmation does not belong to the plan that would be carried out."""


@dataclass(frozen=True)
class Repo:
    full_name: str  # owner/name
    url: str  # the page, for people
    clone_url: str  # where git pushes to


class GitHubClient(Protocol):
    """What publishing needs from GitHub; `RestClient` and `GhClient` implement it, tests fake it."""

    backend: str

    def login(self) -> str: ...

    def create_repo(self, owner: str, name: str, *, private: bool, description: str) -> Repo: ...

    def push_token(self) -> str: ...


# ----------------------------------------------------------------- the plan


@dataclass(frozen=True)
class PublishPlan:
    """Everything the user confirms. `problems` are reasons not to publish; with any, nothing can be sent."""

    workspace: str
    owner: str
    name: str
    visibility: str
    description: str
    backend: str
    branch: str
    commit: str
    tags: tuple[str, ...]
    commits: tuple[str, ...]  # "<short id> <subject>", newest first, at most HISTORY_LIMIT
    commit_count: int
    files: tuple[str, ...]  # every file tracked at the tip: what the repository will contain
    problems: tuple[str, ...]
    removed: tuple[str, ...] = ()  # files in the pushed history that the tip no longer has: sent all the same

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}" if self.owner else self.name

    @property
    def refspecs(self) -> tuple[str, ...]:
        """The exact refs pushed: the planned commit as the branch, and each version tag as it is."""
        return (f"{self.commit}:refs/heads/{self.branch}", *(f"refs/tags/{tag}:refs/tags/{tag}" for tag in self.tags))

    @property
    def fingerprint(self) -> str:
        """What a confirmation must quote: the repository, visibility, branch, commit, and tags."""
        text = "\n".join([self.full_name, self.visibility, self.backend, self.branch, self.commit, *self.tags])
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "full_name": self.full_name, "fingerprint": self.fingerprint, "ok": self.ok}


def default_repo_name(workspace: Path) -> str:
    """The project folder's name without its timestamp, as a valid repository name."""
    name = _FOLDER_PREFIX.sub("", Path(workspace).name)
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")[:100].strip(".-")
    return name or "project"


def _parse_repo(text: str | None, fallback: str, default_owner: str) -> tuple[str, str, str]:
    """(owner, name, problem) of a request: `name` or `owner/name`; an empty request is the default name."""
    text = (text or "").strip()
    owner, name = default_owner, fallback
    if text:
        owner_part, slash, name_part = text.rpartition("/")
        owner, name = (owner_part, name_part) if slash else (default_owner, name_part)
    if (
        not _NAME.match(name)
        or name.endswith(".git")
        or (owner and not _OWNER.match(owner))
    ):
        return "", "", f"{text!r} is not a usable repository name (letters, digits, '.', '-', '_'; optionally owner/name)"
    return owner, name, ""


def _withheld(path: str) -> bool:
    """A path that is never published: an orchestrator record, a secret env file, or git's own folder."""
    parts = path.split("/")
    return ".idea-to-mvp" in parts or ".git" in parts or is_secret_env(parts[-1])


def _git(workspace: Path, *args: str) -> str | None:
    done = run_git(workspace, *args)
    return done.stdout if done is not None and done.returncode == 0 else None


def _git_facts(workspace: Path) -> tuple[dict[str, Any], list[str]]:
    """(branch, commit, tags, commits, count, files) of the delivery's git repository, and the problems with it."""
    git_dir = workspace / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        return {}, ["the project is not a plain git repository, so there is no history to publish"]
    harden_git_dir(git_dir)
    branch = (_git(workspace, "symbolic-ref", "--short", "-q", "HEAD") or "").strip()
    commit = (_git(workspace, "rev-parse", "HEAD") or "").strip()
    if not branch or not commit:
        return {}, ["the project has no commits on a branch yet"]
    listed = (_git(workspace, "tag", "--merged", "HEAD", "--list", "v0.*") or "").split()
    tags = tuple(sorted((tag for tag in listed if _VERSION_TAG.match(tag)), key=lambda tag: int(tag[3:])))
    subjects = (_git(workspace, "log", f"-n{HISTORY_LIMIT}", "--format=%h %s") or "").splitlines()
    count = int((_git(workspace, "rev-list", "--count", "HEAD") or "0").strip() or 0)
    files = tuple(path for path in (_git(workspace, "ls-tree", "-r", "-z", "--name-only", "HEAD") or "").split("\0") if path)
    ever = {path for path in (_git(workspace, "log", "-z", "--name-only", "--format=", "HEAD", *tags) or "").split("\0") if path}
    removed = tuple(sorted(ever - set(files)))
    problems = []
    if held := sorted(path for path in ever if _withheld(path)):
        shown = ", ".join(held[:5]) + (f" and {len(held) - 5} more" if len(held) > 5 else "")
        problems.append(
            f"the git history holds files that must stay local ({shown}); publishing would send them, so it is refused"
        )
    facts = {
        "branch": branch,
        "commit": commit,
        "tags": tags,
        "commits": tuple(subjects),
        "count": count,
        "files": files,
        "removed": removed,
    }
    return facts, problems


def plan_publish(
    state: Mapping[str, Any],
    settings: Settings,
    client: GitHubClient | None,
    *,
    repo: str | None = None,
    visibility: str | None = None,
) -> PublishPlan:
    """What publishing this thread's delivery would do. Reads git and (to learn the account) GitHub; changes nothing."""
    workspace = Path(str(state.get("workspace_dir") or "."))
    problems: list[str] = []
    published = state.get("publication") or {}
    if published:
        problems.append(f"this delivery is already published at {published.get('url') or published.get('repo')}")
    if not state.get("workspace_dir") or not workspace.is_dir() or not state.get("delivery_report"):
        problems.append("there is no delivered project in this session yet")
    visibility = (visibility or settings.publish_visibility or "private").strip().lower()
    if visibility not in VISIBILITIES:
        problems.append(f"visibility must be private or public, not {visibility!r}")

    owner = ""
    if client is None:
        problems.append(NO_ACCESS)
    else:
        try:
            owner = (settings.github_owner or "").strip() or client.login()
        except PublishError as error:
            problems.append(str(error))
    owner, name, bad = _parse_repo(repo, default_repo_name(workspace), owner)
    if bad:
        problems.append(f"repository name: {bad}")

    facts: dict[str, Any] = {}
    if workspace.is_dir() and not any("no delivered project" in problem for problem in problems):
        facts, git_problems = _git_facts(workspace)
        problems.extend(git_problems)
    return PublishPlan(
        workspace=str(workspace),
        owner=owner,
        name=name,
        visibility=visibility,
        description=DESCRIPTION,
        backend=client.backend if client is not None else "",
        branch=facts.get("branch", ""),
        commit=facts.get("commit", ""),
        tags=facts.get("tags", ()),
        commits=facts.get("commits", ()),
        commit_count=int(facts.get("count", 0)),
        files=facts.get("files", ()),
        problems=tuple(problems),
        removed=facts.get("removed", ()),
    )


def render_plan(plan: PublishPlan) -> str:
    """The preview as plain text: the whole of what the user is asked to confirm."""
    if plan.problems:
        return "Cannot publish:\n" + "\n".join(f"  - {problem}" for problem in plan.problems)
    where = {"token": "GITHUB_TOKEN", "gh": "the gh CLI"}.get(plan.backend, plan.backend)
    listed = list(plan.files[:FILE_LIST_LIMIT])
    more = len(plan.files) - len(listed)
    lines = [
        "Publish to GitHub",
        f"  Repository : {plan.full_name} (new, {plan.visibility}) via {where}",
        f"  Description: {plan.description}",
        f"  Branch     : {plan.branch} at {plan.commit[:7]}, {plan.commit_count} commit(s)",
        f"  Tags       : {', '.join(plan.tags) or '(none)'}",
        "  Never sent : .idea-to-mvp/ (orchestrator records), .env files, anything git-ignored, other branches",
        f"  Files ({len(plan.files)}) that will be in the repository:",
        *(f"    {path}" for path in listed),
        *([f"    ... and {more} more"] if more > 0 else []),
        *(
            [
                f"  Also in the history, removed from the latest version ({len(plan.removed)}); they are pushed with it:",
                *(f"    {path}" for path in plan.removed[:FILE_LIST_LIMIT]),
                *([f"    ... and {len(plan.removed) - FILE_LIST_LIMIT} more"] if len(plan.removed) > FILE_LIST_LIMIT else []),
            ]
            if plan.removed
            else []
        ),
        "  History:",
        *(f"    {line}" for line in plan.commits),
        *(["    ..."] if plan.commit_count > len(plan.commits) else []),
    ]
    return "\n".join(lines)


# --------------------------------------------------------------- publishing


def _redact(text: str, secret: str) -> str:
    if not secret:
        return text
    basic = base64.b64encode(f"x-access-token:{secret}".encode()).decode()
    return text.replace(secret, "***").replace(basic, "***")


def _auth_header_key(remote: str) -> str:
    """The git setting that scopes the credential header to the remote's own host (github.com, or the host `gh` is
    signed in to), never to every URL git talks to."""
    host = urllib.parse.urlsplit(remote).hostname if remote.startswith("https://") else None
    return f"http.https://{host or 'github.com'}/.extraheader"


def _push(workspace: Path, remote: str, refspecs: tuple[str, ...], token: str) -> None:
    if not remote or remote.startswith("-"):
        raise PublishError("GitHub returned an unusable remote address")
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": _auth_header_key(remote),
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }
    done = run_git(workspace, "push", "--no-verify", remote, *refspecs, env=env, timeout=900)
    if done is None:
        raise PublishError("git is not available")
    if done.returncode != 0:
        detail = _redact((done.stderr or done.stdout).strip(), token)
        raise PublishError(detail.splitlines()[-1] if detail else f"git push exited {done.returncode}")


def publish_plan(
    plan: PublishPlan,
    client: GitHubClient,
    *,
    confirm: str,
    now: Callable[[], datetime] = datetime.now,
) -> dict[str, Any]:
    """Create the repository and push, for a confirmation that quotes this plan's fingerprint.

    Returns the record kept in the thread's state (`publication`). A failure after the repository exists says so:
    the empty repository is left for the user to delete or push to.
    """
    if not plan.ok:
        raise PublishError("; ".join(plan.problems))
    if not confirm or confirm != plan.fingerprint:
        raise NotConfirmed("what would be published changed since it was reviewed: review it again")
    workspace = Path(plan.workspace)
    git_dir = workspace / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():  # the plan checked this; an agent could have swapped it since
        raise PublishError("the project's .git is not a plain directory, so nothing is published")
    harden_git_dir(git_dir)
    repo = client.create_repo(plan.owner, plan.name, private=plan.visibility == "private", description=plan.description)
    token = client.push_token()
    try:
        _push(workspace, repo.clone_url, plan.refspecs, token)
    except PublishError as error:
        raise PublishError(
            f"created {repo.url} but the push failed: {_redact(str(error), token)}. "
            "The new repository is empty; delete it or push to it yourself."
        ) from None
    return {
        "repo": repo.full_name,
        "url": repo.url,
        "visibility": plan.visibility,
        "branch": plan.branch,
        "commit": plan.commit,
        "tags": list(plan.tags),
        "files": len(plan.files),
        "backend": plan.backend,
        "published_at": now().isoformat(timespec="seconds"),
    }


# ------------------------------------------------------------------ clients

Send = Callable[[str, str, dict[str, str], dict[str, Any] | None], tuple[int, dict[str, Any]]]


def _send(method: str, url: str, headers: dict[str, str], body: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)  # fixed https endpoint
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    except (urllib.error.URLError, TimeoutError) as error:
        raise PublishError(f"could not reach GitHub: {getattr(error, 'reason', error)}") from None
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        if status in (200, 201):
            raise PublishError("GitHub answered with something that is not JSON") from None
        payload = {}
    return status, payload if isinstance(payload, dict) else {}


def _api_error(status: int, body: Mapping[str, Any]) -> str:
    detail = "; ".join(
        [str(body.get("message") or ""), *(str(e.get("message") or e) for e in body.get("errors") or [] if e)]
    ).strip("; ")
    if status == 401:
        return "GitHub rejected the token (401): check GITHUB_TOKEN or sign in again with `gh auth login`"
    if status == 404:
        return "GitHub: the account or organisation was not found, or the token cannot see it (404)"
    if "already exists" in detail:
        return f"GitHub: a repository with that name already exists ({detail})"
    return f"GitHub refused the request ({status}): {detail or 'no detail'}"[:300]


class _Api:
    """The two calls publishing makes, over a transport the subclass provides."""

    backend = ""

    def __init__(self) -> None:
        self._login: str | None = None

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def login(self) -> str:
        if self._login is None:
            self._login = str(self.request("GET", "/user").get("login") or "")
            if not self._login:
                raise PublishError("GitHub did not say whose account this is")
        return self._login

    def create_repo(self, owner: str, name: str, *, private: bool, description: str) -> Repo:
        body = {"name": name, "private": private, "description": description, "auto_init": False}
        mine = owner.lower() == self.login().lower()
        made = self.request("POST", "/user/repos" if mine else f"/orgs/{owner}/repos", body)
        clone_url, url = str(made.get("clone_url") or ""), str(made.get("html_url") or "")
        if not clone_url.startswith("https://") or not url.startswith("https://"):
            raise PublishError("GitHub did not return an https clone address for the new repository")
        return Repo(str(made.get("full_name") or f"{owner}/{name}"), url, clone_url)


class RestClient(_Api):
    """GitHub's REST API with a token (`GITHUB_TOKEN`)."""

    backend = "token"

    def __init__(self, token: str, *, send: Send = _send, api: str = _API) -> None:
        super().__init__()
        self._token = token
        self._send = send
        self._api = api

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "idea-to-mvp",
        }
        status, payload = self._send(method, f"{self._api}{path}", headers, body)
        if status not in (200, 201):
            raise PublishError(_redact(_api_error(status, payload), self._token))
        return payload

    def push_token(self) -> str:
        return self._token


class GhClient(_Api):
    """The signed-in `gh` CLI: its `api` command for GitHub, its `auth token` for the push."""

    backend = "gh"

    def __init__(self, *, run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run, gh: str = "gh") -> None:
        super().__init__()
        self._run = run
        self._gh = gh

    def _call(self, *args: str, body: dict[str, Any] | None = None) -> str:
        try:
            done = self._run(
                [self._gh, *args],
                input=json.dumps(body) if body is not None else None,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise PublishError(f"the gh CLI did not run: {error}") from None
        if done.returncode != 0:
            raise PublishError((done.stderr or done.stdout or "gh failed").strip().splitlines()[-1][:300])
        return done.stdout

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        args = ["api", path] if method == "GET" else ["api", "--method", method, path, "--input", "-"]
        try:
            payload = json.loads(self._call(*args, body=body))
        except ValueError:
            raise PublishError("the gh CLI returned something that is not JSON") from None
        return payload if isinstance(payload, dict) else {}

    def push_token(self) -> str:
        token = self._call("auth", "token").strip()
        if not token:
            raise PublishError("the gh CLI has no token: run `gh auth login`")
        return token


def make_client(settings: Settings, *, which: Callable[[str], str | None] | None = None) -> GitHubClient | None:
    """The way to reach GitHub: the configured token, else the gh CLI; None when there is neither."""
    if token := (settings.github_token or "").strip():
        return RestClient(token)
    if gh := (which or shutil.which)("gh"):
        return GhClient(gh=gh)
    return None


def publish_options(
    state: Mapping[str, Any], settings: Settings, *, which: Callable[[str], str | None] | None = None
) -> dict[str, Any]:
    """What the iterate gate tells the UI about publishing: whether it is possible, the defaults, and what has been
    published already. No network or subprocess: whether `gh` is signed in is found out when a plan is made."""
    backend = "token" if (settings.github_token or "").strip() else "gh" if (which or shutil.which)("gh") else ""
    workspace = str(state.get("workspace_dir") or "")
    delivered = bool(workspace) and Path(workspace).is_dir() and bool(state.get("delivery_report"))
    return {
        "available": bool(backend) and delivered,
        "backend": backend,
        "repo": default_repo_name(Path(workspace)) if workspace else "",
        "visibility": settings.publish_visibility,
        "published": dict(state.get("publication") or {}),
    }

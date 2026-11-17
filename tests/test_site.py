"""The public website (mkdocs.yml, scripts/mkdocs_hooks.py, .github/workflows/pages.yml) builds and links right."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_hooks():
    spec = importlib.util.spec_from_file_location(
        "mkdocs_hooks", ROOT / "scripts" / "mkdocs_hooks.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_site_builds_strictly_and_links_resolve(tmp_path: Path) -> None:
    """A dead link or anchor in the README, the docs, or the pages the hooks add fails the build."""
    result = subprocess.run(
        [sys.executable, "-m", "mkdocs", "build", "--strict", "--site-dir", str(tmp_path / "site")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "NO_MKDOCS_2_WARNING": "1"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    home = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    assert (
        'href="architecture/"' in home
    )  # README's docs/architecture.md, repointed to the site's page
    assert "blob/main/.env.example" in home  # a file the site does not carry goes to the repository
    for page in ("architecture", "security", "contributing", "changelog", "roadmap"):
        assert (tmp_path / "site" / page / "index.html").is_file()


def test_every_page_in_the_nav_exists() -> None:
    nav = (ROOT / "mkdocs.yml").read_text(encoding="utf-8").split("\nnav:\n", 1)[1]
    hooks = load_hooks()
    served = set(hooks.ROOT_PAGES.values())
    for line in nav.splitlines():
        target = line.rsplit(": ", 1)[-1].strip()
        if target.endswith(".md"):
            assert target in served or (ROOT / "docs" / target).is_file(), target


def test_links_are_repointed_to_the_site_or_the_repository() -> None:
    hooks = load_hooks()
    source = ROOT / "README.md"
    text = "\n".join(
        [
            "[arch](docs/architecture.md#pipeline) ![img](docs/assets/panel.png)",
            "[policy](SECURITY.md) [env](.env.example) [ex](examples/link-shortener-fast)",
            "[web](https://example.com/a.md) [anchor](#benchmark) [gone](docs/missing.md) `[x](SECURITY.md)`",
            "```",
            "[fenced](SECURITY.md)",
            "```",
        ]
    )
    out = hooks.repoint(text, source, "index.md")
    assert "[arch](architecture.md#pipeline)" in out and "![img](assets/panel.png)" in out
    assert "[policy](security.md)" in out
    assert f"[env]({hooks.REPOSITORY}/blob/main/.env.example)" in out
    assert f"[ex]({hooks.REPOSITORY}/tree/main/examples/link-shortener-fast)" in out
    # an external link, an in-page anchor, a link to nothing, inline code and a code fence stay as written
    assert "[web](https://example.com/a.md) [anchor](#benchmark) [gone](docs/missing.md)" in out
    assert "`[x](SECURITY.md)`" in out and "[fenced](SECURITY.md)" in out


def test_links_from_a_docs_page_climb_to_the_root_pages() -> None:
    hooks = load_hooks()
    out = hooks.repoint(
        "[readme](../README.md) [roadmap](roadmap.md) [src](../src/idea_to_mvp/cli.py)",
        ROOT / "docs" / "architecture.md",
        "architecture.md",
    )
    assert "[readme](index.md) [roadmap](roadmap.md)" in out
    assert f"{hooks.REPOSITORY}/blob/main/src/idea_to_mvp/cli.py" in out
    nested = hooks.repoint(
        "[up](../../../README.md)", ROOT / "docs/benchmarks/x/README.md", "benchmarks/x/README.md"
    )
    assert nested == "[up](../../index.md)"


def test_the_workflow_deploys_only_from_main_and_builds_strictly() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/pages.yml").read_text(encoding="utf-8"))
    triggers = workflow[True]  # YAML reads the key `on` as a boolean
    assert "pull_request" in triggers and triggers["push"]["branches"] == ["main"]
    steps = " ".join(str(step.get("run", "")) for step in workflow["jobs"]["build"]["steps"])
    assert "mkdocs build --strict" in steps
    deploy = workflow["jobs"]["deploy"]
    assert "refs/heads/main" in deploy["if"] and "pull_request" in deploy["if"]
    assert deploy["permissions"] == {"pages": "write", "id-token": "write"}
    assert workflow["permissions"] == {"contents": "read"}

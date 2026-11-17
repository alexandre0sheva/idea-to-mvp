"""MkDocs hooks for the website: nothing is copied into ``docs/``, the build adapts instead.

- ``README.md`` is the home page, and ``CONTRIBUTING.md``, ``SECURITY.md`` and ``CHANGELOG.md`` are pages
  (``ROOT_PAGES``): the Markdown stays where GitHub renders it, and the site reads it from there.
- A relative Markdown link in any page is repointed at where the site keeps its target: another page or an image
  (the README's ``docs/architecture.md`` becomes ``architecture.md``), or, for a file the site does not carry
  (``src/``, ``examples/``, ``.env.example``), the same path in the repository on GitHub. A link to nothing is left
  as written, so the strict build names it.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from mkdocs.structure.files import File, Files

ROOT = Path(__file__).resolve().parent.parent
REPOSITORY = "https://github.com/alexandre0sheva/idea-to-mvp"
# Files outside docs/ that the site carries: repository path -> page path on the site.
ROOT_PAGES = {
    "README.md": "index.md",
    "CONTRIBUTING.md": "contributing.md",
    "SECURITY.md": "security.md",
    "CHANGELOG.md": "changelog.md",
}
LINK = re.compile(r'(\]\()([^)\s#]*)(#[^)\s]*)?(\s+"[^"]*")?\)')
CODE_SPAN = re.compile(r"(`[^`\n]*`)")
SCHEME = re.compile(r"[a-z][a-z0-9+.-]*:", re.IGNORECASE)


def site_path(path: Path) -> str | None:
    """Where the site keeps the repository file ``path``: a root page, or a file under ``docs/``; else ``None``."""

    relative = path.relative_to(ROOT).as_posix()
    if relative in ROOT_PAGES:
        return ROOT_PAGES[relative]
    docs = (ROOT / "docs").resolve()
    if path.is_relative_to(docs) and path.is_file():
        return path.relative_to(docs).as_posix()
    return None


def on_files(files: Files, config: Any) -> Files:
    for source, target in ROOT_PAGES.items():
        files.append(File.generated(config, target, abs_src_path=str(ROOT / source)))
    return files


def repoint(text: str, source: Path, page_uri: str) -> str:
    """``text`` (the Markdown of repository file ``source``, published as ``page_uri``) with its relative links
    pointing at where the site keeps their targets, or at GitHub."""

    def replace(match: re.Match[str]) -> str:
        opening, target, fragment, title = match.groups()
        if not target or SCHEME.match(target) or target.startswith(("/", "//")):
            return match.group(0)
        resolved = (source.parent / unquote(target)).resolve()
        if not resolved.is_relative_to(ROOT) or not resolved.exists():
            return match.group(0)  # left alone: the strict build reports it
        # A folder whose README the site carries (docs/benchmarks/<run>/) links to that page.
        published = site_path(resolved / "README.md" if resolved.is_dir() else resolved)
        if published is not None:
            url = posixpath.relpath(published, posixpath.dirname(page_uri) or ".")
        else:
            kind = "tree" if resolved.is_dir() else "blob"
            url = f"{REPOSITORY}/{kind}/main/{resolved.relative_to(ROOT).as_posix()}"
        return f"{opening}{url}{fragment or ''}{title or ''})"

    out: list[str] = []
    in_fence = False
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        parts = CODE_SPAN.split(
            line
        )  # inline code is left alone: only the text between spans is rewritten
        out.append("".join(p if i % 2 else LINK.sub(replace, p) for i, p in enumerate(parts)))
    return "".join(out)


def on_page_markdown(markdown: str, page: Any, config: Any, files: Any) -> str:
    return repoint(markdown, Path(page.file.abs_src_path).resolve(), page.file.src_uri)

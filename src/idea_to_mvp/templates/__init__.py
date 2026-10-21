"""Stack starter templates: deterministic scaffolds laid into a workspace before the first task.

Every folder next to this file is one template: `template.toml` (what it is, when to pick it, the toolchain it
needs, and the project commands that work on it) and `files/` (the scaffold, copied as it is). The strategy step
records the matching template in `ExecutionStrategy.template` (`none` when nothing fits); `prepare_workspace`
lays it down and commits `scaffold` before T01; the plan writer is told what already exists and its commands are
the template's, so T01 builds on the scaffold instead of rebuilding it.

A template carries a test runner, a linter, a test command that writes the JUnit report the orchestrator reads
(`plan.commands.test_report`), a `.gitignore`, and `scripts/check.sh` rendered from those commands (the same
renderer the blueprint pack uses, so the two are the same file). The scaffold is never executed by the
orchestrator: only an agent session, in its sandbox, installs and runs it.

Layout rules: `{{name}}` in a file is replaced by the project's name; a file called `gitignore` becomes
`.gitignore` (a real `.gitignore` here would apply to this folder); a file the pack already has is left alone
(the pack's documents win), except `.gitignore`, whose lines are merged.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from idea_to_mvp.harness_pack import CHECK_SCRIPT, render_check_script
from idea_to_mvp.plan import Plan, ProjectCommands

LOGGER = logging.getLogger(__name__)

NONE = "none"  # the template of a project nothing fits
TEMPLATES_DIR = Path(__file__).resolve().parent
_SPEC_FILE = "template.toml"
_FILES_DIR = "files"
_RENAMED = {"gitignore": ".gitignore"}
_NAME_TOKEN = "{{name}}"
_JUNK = {
    "__pycache__",
    ".ruff_cache",
    ".pytest_cache",
    ".mypy_cache",
    ".venv",
    "node_modules",
    ".DS_Store",
}  # a tool run in the tree
_PACK_TIMESTAMP = re.compile(r"^\d{8}-\d{6}-\d{6}-")  # the blueprint folder's `<timestamp>-` prefix
STRATEGY_FILE = "STRATEGY.json"  # the pack file that records the choice


@dataclass(frozen=True)
class Template:
    name: str
    title: str
    description: str
    use_when: str  # when the strategy step should pick it
    toolchain: tuple[
        str, ...
    ]  # executables the template's commands need on the machine (`node`, `python3`, ...)
    notes: tuple[str, ...]  # how the scaffold is laid out, for the plan writer
    commands: ProjectCommands
    root: Path  # the folder with the scaffold files


def _load(folder: Path) -> Template:
    data = tomllib.loads((folder / _SPEC_FILE).read_text(encoding="utf-8"))
    commands = data["commands"]
    return Template(
        name=str(data["name"]),
        title=str(data["title"]),
        description=str(data["description"]),
        use_when=str(data["use_when"]),
        toolchain=tuple(data["toolchain"]),
        notes=tuple(data.get("notes", [])),
        commands=ProjectCommands(
            install=commands.get("install"),
            test=commands["test"],
            lint=commands.get("lint"),
            run=commands.get("run"),
            test_report=commands.get("test_report"),
        ),
        root=folder / _FILES_DIR,
    )


@lru_cache(maxsize=1)
def load_templates() -> dict[str, Template]:
    """Every template, by name."""
    found = [
        _load(folder)
        for folder in sorted(TEMPLATES_DIR.iterdir())
        if (folder / _SPEC_FILE).is_file()
    ]
    return {template.name: template for template in found}


def template_names() -> list[str]:
    return sorted(load_templates())


def get_template(name: str | None) -> Template | None:
    """The template called `name`; None for `none`, an empty value, or a name that is not a template."""
    return load_templates().get(str(name or "").strip().lower())


def normalize_template(value: object) -> str:
    """A template name for the strategy: a known one, else `none` (a model's guess must not break the run)."""
    template = get_template(value if isinstance(value, str) else None)
    return template.name if template else NONE


# ------------------------------------------------------------------ files


def project_name(folder_name: str) -> str:
    """A package-safe name for the project from its blueprint folder (`20261020-101500-000001-habit-tracker`)."""
    slug = re.sub(r"[^a-z0-9]+", "-", _PACK_TIMESTAMP.sub("", folder_name).lower()).strip("-")
    return slug or "app"


def template_files(template: Template, name: str = "app") -> dict[str, str]:
    """The scaffold as path -> text, with the project `name` filled in and `scripts/check.sh` rendered from the
    template's commands."""
    files: dict[str, str] = {}
    for path in sorted(template.root.rglob("*")):
        if path.is_file() and not _JUNK.intersection(path.relative_to(template.root).parts):
            relative = path.relative_to(template.root)
            target = relative.with_name(_RENAMED.get(relative.name, relative.name)).as_posix()
            files[target] = path.read_text(encoding="utf-8").replace(_NAME_TOKEN, name)
    files[CHECK_SCRIPT] = render_check_script(template.commands)
    return files


def _merged_gitignore(current: str, template_lines: str) -> str:
    lines = current.splitlines()
    missing = [line for line in template_lines.splitlines() if line.strip() and line not in lines]
    return "\n".join([*lines, *missing]) + "\n"


def _writable(workspace: Path, relative: str) -> bool:
    """False when the file exists or a link (the pack could contain one) lies on its path."""
    path = workspace
    for part in Path(relative).parts:
        path = path / part
        if path.is_symlink():
            return False
    return not os.path.lexists(path)


def apply_template(template: Template, workspace: Path, name: str | None = None) -> list[str]:
    """Lay the scaffold into `workspace`; returns the paths written. Files the workspace already has are kept
    (except `.gitignore`, which gets the template's lines added)."""
    workspace = Path(workspace)
    written: list[str] = []
    for relative, text in template_files(template, name or project_name(workspace.name)).items():
        target = workspace / relative
        if relative == ".gitignore" and target.is_file() and not target.is_symlink():
            target.write_text(
                _merged_gitignore(target.read_text(encoding="utf-8"), text), encoding="utf-8"
            )
            written.append(relative)
            continue
        if not _writable(workspace, relative):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text.rstrip("\n") + "\n", encoding="utf-8")
        if relative == CHECK_SCRIPT:
            target.chmod(0o755)
        written.append(relative)
    return written


def pack_template(bundle_dir: Path) -> Template | None:
    """The template a blueprint pack asks for (from its `STRATEGY.json`); None when it records none, or a
    template this version does not have."""
    try:
        recorded = json.loads((Path(bundle_dir) / STRATEGY_FILE).read_text(encoding="utf-8")).get(
            "template"
        )
    except (OSError, ValueError, AttributeError):
        return None
    if not recorded or str(recorded).strip().lower() == NONE:
        return None
    template = get_template(str(recorded))
    if template is None:
        LOGGER.warning(
            "The pack asks for the starter template %r, which this version does not have; skipped.",
            recorded,
        )
    return template


# ------------------------------------------------------------------ prompts and the plan


def catalog_text() -> str:
    """The templates the strategy step may pick from, one bullet each."""
    lines = [
        f"- `{t.name}` ({t.title}): {t.description} Pick it when: {t.use_when}"
        for t in load_templates().values()
    ]
    return "\n".join(
        lines
        + [
            f"- `{NONE}`: nothing above fits the chosen architecture's stack (or the user's preferences)."
        ]
    )


def prompt_block(name: str | None) -> str:
    """What the blueprint documents are told about the template ('' for none): that its files exist before T01,
    what they are, and that the plan's commands are its commands."""
    template = get_template(name)
    if template is None:
        return ""
    paths = "\n".join(f"- `{path}`" for path in template_files(template))
    commands = "\n".join(
        f"- {label}: `{value}`"
        for label, value in (
            ("install", template.commands.install),
            ("test", template.commands.test),
            ("test_report", template.commands.test_report),
            ("lint", template.commands.lint),
            ("run", template.commands.run),
        )
        if value
    )
    notes = "\n".join(f"- {note}" for note in template.notes)
    return (
        f"Starter template `{template.name}` ({template.title}) is already in the workspace before the first task, "
        f"committed as `scaffold`:\n{template.description}\n\nFiles it provides:\n{paths}\n\n"
        f"How it is laid out:\n{notes}\n\n"
        f"Its project commands are fixed; use exactly these in the plan:\n{commands}\n\n"
        "Do not plan tasks that create, configure, or re-create the scaffold (the project skeleton, the test "
        "runner, the linter, `.gitignore`, `scripts/check.sh`). T01 starts from it: it adds the product's first "
        "slice on top and changes the scaffold's files only where the product needs it.\n\n"
    )


def with_template_commands(plan: Plan, name: str | None) -> Plan:
    """The plan with the template's commands: the scaffold's config files assume exactly these, so a plan that
    names others would send the agents against a project that does not match them."""
    template = get_template(name)
    if template is None:
        return plan
    return plan.model_copy(update={"commands": template.commands})


# ------------------------------------------------------------------ toolchain


def missing_tools(
    template: Template, which: Callable[[str], str | None] = shutil.which
) -> list[str]:
    """The executables the template needs that are not on the PATH."""
    return [tool for tool in template.toolchain if which(tool) is None]

"""Stack starter templates: the registry, how a scaffold is laid into a workspace, and (opt-in, `-m scaffold`)
that each one's `scripts/check.sh` really runs green with the real toolchain."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from plan_helpers import make_plan

from idea_to_mvp.harness_pack import (
    CHECK_SCRIPT,
    REPORTS_DIR,
    render_check_script,
    validate_harness,
)
from idea_to_mvp.implementation.reports import read_evidence
from idea_to_mvp.plan import validate_plan
from idea_to_mvp.templates import (
    NONE,
    apply_template,
    catalog_text,
    get_template,
    load_templates,
    missing_tools,
    normalize_template,
    pack_template,
    project_name,
    prompt_block,
    template_files,
    template_names,
    with_template_commands,
)

EXPECTED = ["nextjs", "node-ts-express", "python-cli", "python-fastapi", "react-vite"]


def test_the_library_has_the_five_stacks() -> None:
    assert template_names() == EXPECTED


@pytest.mark.parametrize("name", EXPECTED)
def test_every_template_can_run_tests_with_a_report_and_lint(name: str) -> None:
    template = get_template(name)
    assert template is not None and template.name == name
    commands = template.commands
    assert commands.install and commands.lint and commands.run and commands.test
    assert REPORTS_DIR in (commands.test_report or "")  # the folder the tests lane reads
    assert "junit" in (commands.test_report or "")
    plan = with_template_commands(make_plan(), name)
    assert plan.commands == commands
    problems = validate_plan(plan, prd_markdown="## Requirements\n- R1 (P0): x\n- R2 (P0): y\n- R3 (P1): z\n", workstreams=["backend-api", "web-ui"])
    assert not [p for p in problems if "command" in p]


@pytest.mark.parametrize("name", EXPECTED)
def test_a_scaffold_carries_a_gitignore_a_check_script_and_a_test(name: str) -> None:
    template = get_template(name)
    assert template is not None
    files = template_files(template, "habit-tracker")
    assert ".gitignore" in files and "gitignore" not in files  # a real .gitignore here would apply to this folder
    assert files[CHECK_SCRIPT] == render_check_script(template.commands)
    assert files[CHECK_SCRIPT].startswith("#!/usr/bin/env bash")
    assert any("test" in path and path != CHECK_SCRIPT for path in files)
    assert not [path for path, text in files.items() if "{{name}}" in text]  # every placeholder is filled
    assert any("habit-tracker" in text for text in files.values())
    assert "node_modules/" in files[".gitignore"] or ".venv/" in files[".gitignore"]
    assert not [path for path in files if path in {"README.md", "AGENTS.md", "CLAUDE.md"}]  # the pack's documents


def test_a_scaffold_never_brings_a_file_outside_the_harness_surface_into_conflict() -> None:
    """The check script and the gitignore are the only files a pack also writes; the rest is the stack's own."""
    pack_files = {"CLAUDE.md", "AGENTS.md", "README.md", "PRD.md", "ARCHITECTURE.md", "plan.md", "plan.json", "STRATEGY.json"}
    for template in load_templates().values():
        clashes = set(template_files(template)) & pack_files
        assert not clashes, (template.name, clashes)


def test_the_check_script_of_a_template_is_the_one_a_pack_renders_for_its_commands() -> None:
    """Pack and template then write the same bytes, whichever lands first."""
    for template in load_templates().values():
        assert template_files(template)[CHECK_SCRIPT] == render_check_script(with_template_commands(make_plan(), template.name).commands)


def test_the_harness_validator_accepts_the_check_script_of_every_template() -> None:
    from idea_to_mvp.harness_pack import PROJECT_SETTINGS, fallback_harness, render_harness

    for template in load_templates().values():
        plan = with_template_commands(make_plan(), template.name)
        files = render_harness(fallback_harness(plan), plan, "# AGENTS\n")
        assert files[CHECK_SCRIPT] == template_files(template)[CHECK_SCRIPT]
        assert validate_harness(files) == []
        assert json.loads(files[".claude/settings.json"]) == PROJECT_SETTINGS


# --------------------------------------------------------------------------- the registry


def test_a_model_guess_that_is_not_a_template_becomes_none() -> None:
    assert normalize_template("python-fastapi") == "python-fastapi"
    assert normalize_template("  Python-FastAPI ") == "python-fastapi"
    for value in ("django", "", None, 3, "none"):
        assert normalize_template(value) == NONE
    assert get_template("none") is None and get_template(None) is None


def test_the_strategy_is_offered_every_template_and_none() -> None:
    text = catalog_text()
    for name in EXPECTED:
        assert f"`{name}`" in text
    assert f"`{NONE}`" in text


def test_the_project_name_drops_the_pack_timestamp() -> None:
    assert project_name("20261020-101500-000001-habit-tracker") == "habit-tracker"
    assert project_name("20261020-101500-000001-habit-tracker-101500123456") == "habit-tracker-101500123456"
    assert project_name("20261020-101500-000001-") == "app"
    assert project_name("My Idea!") == "my-idea"


def test_missing_tools_names_what_the_template_needs(monkeypatch: pytest.MonkeyPatch) -> None:
    node = get_template("react-vite")
    assert node is not None
    assert missing_tools(node, which=lambda tool: None) == ["node", "npm"]
    assert missing_tools(node, which=lambda tool: f"/bin/{tool}") == []
    assert missing_tools(node, which=lambda tool: None if tool == "npm" else "/bin/node") == ["npm"]


def test_the_plan_writer_is_told_what_already_exists() -> None:
    block = prompt_block("python-fastapi")
    assert "python-fastapi" in block and "`src/app/main.py`" in block and "`scripts/check.sh`" in block
    assert "Do not plan tasks that create" in block and "scaffold" in block
    assert (get_template("python-fastapi").commands.test_report or "") in block  # type: ignore[union-attr]
    assert prompt_block("none") == "" and prompt_block(None) == "" and prompt_block("django") == ""


def test_a_plan_keeps_its_own_commands_without_a_template() -> None:
    plan = make_plan()
    assert with_template_commands(plan, "none") is plan
    assert with_template_commands(plan, None) is plan


# ------------------------------------------------------------------------- laying it down


def _pack(tmp_path: Path, template: str | None) -> Path:
    pack = tmp_path / "20261020-101500-000001-habit-tracker"
    pack.mkdir()
    (pack / "README.md").write_text("# the pack's own README\n")
    strategy: dict = {"mode": "agent_team", "reasoning": "r", "workstreams": []}
    if template is not None:
        strategy["template"] = template
    (pack / "STRATEGY.json").write_text(json.dumps(strategy))
    return pack


def test_apply_lays_the_scaffold_and_keeps_what_the_workspace_has(tmp_path: Path) -> None:
    workspace = tmp_path / "20261020-101500-000001-habit-tracker"
    workspace.mkdir()
    (workspace / "README.md").write_text("pack readme\n")
    (workspace / "scripts").mkdir()
    (workspace / "scripts" / "check.sh").write_text("#!/bin/sh\n# the pack's own\n")
    (workspace / ".gitignore").write_text("custom/\n.venv/\n")
    template = get_template("python-cli")
    assert template is not None
    written = apply_template(template, workspace)
    assert "pyproject.toml" in written and "src/app/cli.py" in written
    assert CHECK_SCRIPT not in written  # the pack's file stays
    assert (workspace / "scripts" / "check.sh").read_text() == "#!/bin/sh\n# the pack's own\n"
    assert (workspace / "README.md").read_text() == "pack readme\n"
    ignore = (workspace / ".gitignore").read_text().splitlines()
    assert ignore[:2] == ["custom/", ".venv/"] and ignore.count(".venv/") == 1 and "__pycache__/" in ignore
    assert 'name = "habit-tracker"' in (workspace / "pyproject.toml").read_text()


def test_apply_makes_the_check_script_executable(tmp_path: Path) -> None:
    template = get_template("node-ts-express")
    assert template is not None
    apply_template(template, tmp_path)
    assert os.access(tmp_path / CHECK_SCRIPT, os.X_OK)


def test_apply_never_writes_through_a_link_in_the_pack(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "src").symlink_to(outside, target_is_directory=True)
    (workspace / "pyproject.toml").symlink_to(outside / "elsewhere.toml")  # a dangling link
    template = get_template("python-cli")
    assert template is not None
    written = apply_template(template, workspace)
    assert "src/app/cli.py" not in written and "pyproject.toml" not in written
    assert list(outside.iterdir()) == []


# ------------------------------------------------------------------------- the pack's choice


def test_the_pack_names_its_template_in_strategy_json(tmp_path: Path) -> None:
    template = pack_template(_pack(tmp_path, "react-vite"))
    assert template is not None and template.name == "react-vite"


@pytest.mark.parametrize("recorded", [None, "none", "", "django"])
def test_no_template_when_the_pack_records_none_or_an_unknown_one(tmp_path: Path, recorded: str | None) -> None:
    assert pack_template(_pack(tmp_path, recorded)) is None


def test_a_pack_without_strategy_json_has_no_template(tmp_path: Path) -> None:
    assert pack_template(tmp_path) is None
    (tmp_path / "STRATEGY.json").write_text("not json")
    assert pack_template(tmp_path) is None
    (tmp_path / "STRATEGY.json").write_text("[1]")
    assert pack_template(tmp_path) is None


# ------------------------------------------------- the real toolchain (opt-in: `pytest -m scaffold`)


@pytest.mark.scaffold
@pytest.mark.parametrize("name", EXPECTED)
def test_check_sh_runs_green_on_the_scaffold(name: str, tmp_path: Path) -> None:
    """Install, lint, and test the bare scaffold the way an agent session would. Needs network access for the
    package install; CI runs it, a machine without the toolchain skips it."""
    template = get_template(name)
    assert template is not None
    if missing_tools(template):
        pytest.skip(f"{', '.join(missing_tools(template))} not installed")
    apply_template(template, tmp_path, "demo-app")
    done = subprocess.run(
        ["bash", CHECK_SCRIPT], cwd=tmp_path, capture_output=True, text=True, timeout=900, check=False
    )
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    evidence = read_evidence(tmp_path)
    assert evidence.problem == "" and evidence.tests >= 1 and evidence.failed == 0 and evidence.errors == 0
    if name.startswith("python"):  # the Cobertura file of the JS tools has a DOCTYPE, which the reader refuses
        assert evidence.coverage_percent is not None


def test_what_a_tool_leaves_in_the_template_folder_is_not_part_of_the_scaffold() -> None:
    template = get_template("python-cli")
    assert template is not None
    for junk in (".ruff_cache/CACHEDIR.TAG", "src/app/__pycache__/cli.pyc", "node_modules/x/index.js"):
        stray = template.root / junk
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_bytes(b"\xff\xff")
    try:
        assert not [path for path in template_files(template) if "cache" in path or "node_modules" in path]
    finally:
        for top in (".ruff_cache", "node_modules"):
            shutil.rmtree(template.root / top, ignore_errors=True)
        shutil.rmtree(template.root / "src" / "app" / "__pycache__", ignore_errors=True)

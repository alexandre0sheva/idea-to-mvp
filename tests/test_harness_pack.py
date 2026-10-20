"""The agent harness of a generated project: rendering, the deterministic validator, and settings restore."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import BaseMessage
from plan_helpers import make_plan

from idea_to_mvp import harness_pack as hp
from idea_to_mvp import llm
from idea_to_mvp.config import clear_settings_cache
from idea_to_mvp.demo.fixtures import harness_spec as demo_harness_spec
from idea_to_mvp.harness_pack import HarnessSpec, SkillSpec
from idea_to_mvp.nodes.blueprint import DocInput, generate_harness_node
from idea_to_mvp.plan import ProjectCommands

AGENTS_MD = "# Project Operating Guide\n\n- Read `PRD.md` first.\n"


def skill(name: str = "add-endpoint", description: str = "Use when adding an HTTP endpoint.", body: str = "") -> SkillSpec:
    return SkillSpec(name=name, description=description, body=body or "1. Add the route.\n2. Run `bash scripts/check.sh`.")


def spec(**overrides: Any) -> HarnessSpec:
    fields: dict[str, Any] = {"skills": [skill()], "tool_guidance": ["Use `pytest -k name` to run one test."], "guardrails": []}
    fields.update(overrides)
    return HarnessSpec(**fields)


def plan_with(**commands: Any):
    defaults: dict[str, Any] = {"install": "pip install -e .", "test": "pytest", "lint": "ruff check .", "run": "python -m app"}
    defaults.update(commands)
    defaults.setdefault("test_report", "pytest --junitxml=.idea-to-mvp/reports/junit.xml")
    return make_plan(commands=ProjectCommands(**defaults))


def rendered(harness: HarnessSpec | None = None, plan=None) -> dict[str, str]:
    return hp.render_harness(harness or spec(), plan or plan_with(), AGENTS_MD)


# ------------------------------------------------------------------- rendering


def test_a_rendered_harness_has_every_component_and_validates() -> None:
    files = rendered()
    assert {hp.CLAUDE_MD, hp.CHECK_SCRIPT, hp.SETTINGS_PATH, "AGENTS.md", ".claude/skills/add-endpoint/SKILL.md"} <= set(files)
    assert hp.validate_harness(files) == []
    assert json.loads(files[hp.SETTINGS_PATH]) == hp.PROJECT_SETTINGS
    assert "hooks" not in files[hp.SETTINGS_PATH] and "permissions" not in files[hp.SETTINGS_PATH]


def test_claude_md_points_to_agents_md_and_lists_the_commands_skills_and_tool_guidance() -> None:
    claude = rendered()[hp.CLAUDE_MD]
    assert "`AGENTS.md`" in claude.split("## Commands")[0]  # the pointer comes first
    for command in ("bash scripts/check.sh", "pip install -e .", "ruff check .", "python -m app"):
        assert f"`{command}`" in claude, command
    assert "`add-endpoint`: Use when adding an HTTP endpoint." in claude
    assert "Use `pytest -k name` to run one test." in claude


def test_the_guardrails_section_is_appended_once_to_agents_md_with_the_stack_rules() -> None:
    files = rendered(spec(guardrails=["Never call the payment API from the UI."]))
    agents = files["AGENTS.md"]
    assert agents.startswith(AGENTS_MD.strip()) and agents.count("## Guardrails") == 1
    assert "Never call the payment API from the UI." in agents and "`bash scripts/check.sh`" in agents
    again = hp.render_harness(spec(), plan_with(), agents)["AGENTS.md"]  # re-rendering replaces the section
    assert again.count("## Guardrails") == 1 and "payment API" not in again


def test_a_skill_description_with_colons_and_quotes_survives_its_frontmatter() -> None:
    tricky = skill(description='Use when a route needs auth: add "require_user" first.')
    text = rendered(spec(skills=[tricky]))[".claude/skills/add-endpoint/SKILL.md"]
    meta, body = hp.parse_skill(text)
    assert meta == {"name": "add-endpoint", "description": 'Use when a route needs auth: add "require_user" first.'}
    assert body.startswith("1. Add the route.")


def test_the_fallback_harness_is_valid_for_a_plan_without_optional_commands() -> None:
    bare = plan_with(install=None, lint=None, run=None, test_report=None)
    files = hp.render_harness(hp.fallback_harness(bare), bare, AGENTS_MD)
    assert hp.validate_harness(files) == []
    assert ".claude/skills/check/SKILL.md" in files and not any("run-the-app" in path for path in files)
    full = plan_with()
    assert ".claude/skills/run-the-app/SKILL.md" in hp.render_harness(hp.fallback_harness(full), full, AGENTS_MD)


def test_the_demo_fixture_harness_is_valid() -> None:
    plan = plan_with()
    assert hp.validate_harness(hp.render_harness(demo_harness_spec([]), plan, AGENTS_MD)) == []


# ------------------------------------------------------------------- validator


def test_a_skill_without_a_trigger_is_refused() -> None:
    problems = hp.validate_harness(rendered(spec(skills=[skill(description="Adds an HTTP endpoint.")])))
    assert any("add-endpoint" in p and "trigger" in p for p in problems)


def test_a_skill_whose_frontmatter_name_differs_from_its_folder_is_refused() -> None:
    files = rendered()
    files[".claude/skills/other-name/SKILL.md"] = files.pop(".claude/skills/add-endpoint/SKILL.md")
    assert any("other-name" in p and "name" in p for p in hp.validate_harness(files))


def test_a_skill_without_frontmatter_or_body_is_refused() -> None:
    files = rendered()
    files[".claude/skills/add-endpoint/SKILL.md"] = "Just prose, no frontmatter."
    assert any("frontmatter" in p for p in hp.validate_harness(files))
    empty = rendered(spec(skills=[skill(body=" ")]))
    assert any("no steps" in p for p in hp.validate_harness(empty))


def test_a_skill_that_invokes_a_missing_script_is_refused_until_the_script_exists() -> None:
    files = rendered(spec(skills=[skill(body="1. Run `scripts/migrate.sh up`.\n2. Run `bash scripts/check.sh`.")]))
    problems = hp.validate_harness(files)
    assert problems == [".claude/skills/add-endpoint/SKILL.md invokes scripts/migrate.sh, which does not exist"]
    files["scripts/migrate.sh"] = "#!/usr/bin/env bash\necho migrate\n"
    assert hp.validate_harness(files) == []


def test_a_script_the_plans_own_commands_name_is_the_projects_to_provide() -> None:
    plan = plan_with(test="python scripts/run_tests.py", test_report="python scripts/run_tests.py --junit", run="bash scripts/serve.sh")
    uses_it = spec(skills=[skill(body="1. Start it with `bash scripts/serve.sh`.\n2. Run `bash scripts/check.sh`.")])
    assert hp.validate_harness(hp.render_harness(uses_it, plan, AGENTS_MD)) == []
    fallback = hp.render_harness(hp.fallback_harness(plan), plan, AGENTS_MD)
    assert hp.validate_harness(fallback) == []
    unknown = spec(skills=[skill(body="Run `scripts/seed.py`, then `bash scripts/check.sh`.")])
    assert hp.validate_harness(hp.render_harness(unknown, plan, AGENTS_MD)) == [
        ".claude/skills/add-endpoint/SKILL.md invokes scripts/seed.py, which does not exist"
    ]


def test_a_folder_named_in_passing_is_not_an_invoked_script() -> None:
    files = rendered(spec(skills=[skill(body="1. Put migrations in `scripts/migrations/`.\n2. Run `bash scripts/check.sh`.")]))
    assert hp.validate_harness(files) == []


def test_too_many_skills_are_refused() -> None:
    many = [skill(name=f"skill-{i}") for i in range(hp.MAX_SKILLS + 1)]
    assert any("at most" in p for p in hp.validate_harness(rendered(spec(skills=many))))


def test_always_on_text_over_its_budget_is_refused() -> None:
    long_guidance = ["Use the package manager. " * 20] * 20
    assert any("always-on" in p for p in hp.validate_harness(rendered(spec(tool_guidance=long_guidance))))
    long_rules = ["Never do this particular thing in this stack. " * 6] * 4
    assert any("guardrails" in p for p in hp.validate_harness(rendered(spec(guardrails=long_rules))))


@pytest.mark.parametrize(
    "path",
    ["src/app.py", "../outside.md", "/etc/CLAUDE.md", ".claude/settings.local.json", ".claude/hooks/pre.sh", ".mcp.json",
     "scripts/nested/run.sh", ".claude/skills/Bad_Name/SKILL.md", "scripts\\check.sh"],
)  # fmt: skip
def test_files_outside_the_harness_surface_are_refused(path: str) -> None:
    files = rendered()
    files[path] = "x\n"
    assert any(p.startswith(f"{path}: outside the harness surface") for p in hp.validate_harness(files))


def test_project_settings_may_define_no_hooks_or_permissions() -> None:
    for tampered in ({"hooks": {"PreToolUse": []}}, {"permissions": {"allow": ["Bash"]}}, {"enableAllProjectMcpServers": True}):
        files = rendered()
        files[hp.SETTINGS_PATH] = json.dumps({**hp.PROJECT_SETTINGS, **tampered})
        assert any(p.startswith(hp.SETTINGS_PATH) for p in hp.validate_harness(files)), tampered
    files = rendered()
    files[hp.SETTINGS_PATH] = "{not json"
    assert any(p.startswith(hp.SETTINGS_PATH) for p in hp.validate_harness(files))


def test_the_required_components_must_be_there() -> None:
    files = rendered()
    for required in (hp.CLAUDE_MD, hp.CHECK_SCRIPT, hp.SETTINGS_PATH):
        partial = {path: text for path, text in files.items() if path != required}
        assert any(required in p and "missing" in p for p in hp.validate_harness(partial)), required
    no_section = {**files, "AGENTS.md": AGENTS_MD}
    assert any("AGENTS.md" in p and "guardrails" in p for p in hp.validate_harness(no_section))
    no_skills = {path: text for path, text in files.items() if "/skills/" not in path}
    assert any("skill" in p for p in hp.validate_harness(no_skills))


# --------------------------------------------------------------- scripts/check.sh

pytestmark_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def run_check(tmp_path: Path, plan, *steps: str) -> tuple[int, list[str]]:
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True, exist_ok=True)
    script = project / hp.CHECK_SCRIPT
    script.write_text(hp.render_check_script(plan.commands), encoding="utf-8")
    log = project / "log.txt"
    log.unlink(missing_ok=True)
    done = subprocess.run(["bash", str(script), *steps], cwd=tmp_path, capture_output=True, text=True, timeout=30, check=False)
    return done.returncode, log.read_text().split() if log.exists() else []


def echo_plan(*, lint: str = "echo lint >> log.txt", install: str | None = "echo install >> log.txt", report: str | None = "echo report >> log.txt"):
    return plan_with(install=install, lint=lint, test="echo test >> log.txt", test_report=report)


@pytestmark_bash
def test_check_runs_install_lint_and_the_report_command_from_the_project_root(tmp_path: Path) -> None:
    assert run_check(tmp_path, echo_plan()) == (0, ["install", "lint", "report"])  # cwd was elsewhere
    assert (tmp_path / "project" / ".idea-to-mvp" / "reports").is_dir()  # the report folder exists for the command


@pytestmark_bash
def test_check_runs_the_plain_tests_without_a_report_command_and_only_the_steps_named(tmp_path: Path) -> None:
    assert run_check(tmp_path, echo_plan(report=None)) == (0, ["install", "lint", "test"])
    assert run_check(tmp_path, echo_plan(), "test") == (0, ["report"])
    assert run_check(tmp_path, echo_plan(), "install", "test") == (0, ["install", "report"])
    assert run_check(tmp_path, echo_plan(install=None))[1] == ["lint", "report"]


@pytestmark_bash
def test_a_cd_or_an_exit_in_one_step_does_not_leak_into_the_next(tmp_path: Path) -> None:
    (tmp_path / "project" / "web").mkdir(parents=True)
    plan = echo_plan(lint="cd web && echo lint >> ../log.txt", install="echo install >> log.txt; exit 0")
    assert run_check(tmp_path, plan) == (0, ["install", "lint", "report"])  # the report still ran from the root
    assert (tmp_path / "project" / ".idea-to-mvp" / "reports").is_dir()


@pytestmark_bash
def test_a_failed_lint_still_runs_the_tests_but_a_failed_install_stops(tmp_path: Path) -> None:
    assert run_check(tmp_path, echo_plan(lint="echo lint >> log.txt; false")) == (1, ["install", "lint", "report"])
    assert run_check(tmp_path, echo_plan(install="echo install >> log.txt; false")) == (1, ["install"])
    assert run_check(tmp_path, echo_plan(), "deploy")[0] == 2


# ------------------------------------------------------ the workspace's harness on disk


def write_files(root: Path, files: dict[str, str]) -> None:
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def test_read_harness_reads_the_surface_only_and_never_follows_a_symlink(tmp_path: Path) -> None:
    write_files(tmp_path, {**rendered(), "src/app.py": "print()\n", "PRD.md": "# PRD\n"})
    (tmp_path / "outside.md").write_text("secret\n")
    (tmp_path / ".claude" / "skills" / "linked").mkdir(parents=True)
    os.symlink(tmp_path / "outside.md", tmp_path / ".claude" / "skills" / "linked" / "SKILL.md")
    files = hp.read_harness(tmp_path)
    assert files == rendered()
    assert hp.validate_harness(files) == []


def test_ensure_project_settings_restores_what_an_agent_changed(tmp_path: Path) -> None:
    settings = tmp_path / hp.SETTINGS_PATH
    assert hp.ensure_project_settings(tmp_path) is False and not settings.exists()  # no file loads nothing
    write_files(tmp_path, {hp.SETTINGS_PATH: json.dumps(hp.PROJECT_SETTINGS, indent=2) + "\n"})
    assert hp.ensure_project_settings(tmp_path) is False  # canonical already: untouched
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "evil"}]}]}}))
    assert hp.ensure_project_settings(tmp_path) is True
    assert json.loads(settings.read_text()) == hp.PROJECT_SETTINGS


def test_ensure_project_settings_removes_symlinks_instead_of_writing_through_them(tmp_path: Path) -> None:
    workspace, elsewhere = tmp_path / "ws", tmp_path / "elsewhere"
    workspace.mkdir()
    elsewhere.mkdir()
    (elsewhere / "settings.json").write_text("{}")
    os.symlink(elsewhere, workspace / ".claude")
    assert hp.ensure_project_settings(workspace) is True
    assert not (workspace / ".claude").exists() and (elsewhere / "settings.json").read_text() == "{}"
    target = workspace / hp.SETTINGS_PATH
    target.parent.mkdir()
    os.symlink(elsewhere / "settings.json", target)
    assert hp.ensure_project_settings(workspace) is True
    assert not target.is_symlink() and json.loads(target.read_text()) == hp.PROJECT_SETTINGS
    assert (elsewhere / "settings.json").read_text() == "{}"


def test_refresh_commands_follows_an_edited_plan(tmp_path: Path) -> None:
    write_files(tmp_path, rendered())
    edited = plan_with(test="pytest -q", test_report="pytest -q --junitxml=.idea-to-mvp/reports/junit.xml", run="uvicorn app:api")
    hp.refresh_commands(tmp_path, edited)
    claude = (tmp_path / hp.CLAUDE_MD).read_text()
    assert "`uvicorn app:api`" in claude and "`python -m app`" not in claude and claude.count("## Commands") == 1
    assert "`add-endpoint`" in claude  # the rest of CLAUDE.md is kept
    assert "pytest -q --junitxml" in (tmp_path / hp.CHECK_SCRIPT).read_text()
    assert os.access(tmp_path / hp.CHECK_SCRIPT, os.X_OK)
    hp.refresh_commands(tmp_path / "missing", edited)  # a pack without a harness is left alone


# ------------------------------------------------------------ the harness writer node


class FakeRuntime:
    def __init__(self, role: str) -> None:
        self.role, self.llm, self.provider, self.model = role, object(), "anthropic", "fake"
        self.max_tokens, self.system_prompt = 256, f"system::{role}"


class Writer:
    """Scripted harness-writer answers (None means the model failed: the fallback is returned)."""

    def __init__(self) -> None:
        self.answers: list[HarnessSpec | None] = []
        self.prompts: list[list[BaseMessage]] = []

    def invoke_structured(self, runtime: Any, messages: list[BaseMessage], schema: Any, *, fallback: Any = None) -> Any:
        assert schema is HarnessSpec and runtime.role == "harness_writer"
        self.prompts.append(messages)
        answer = self.answers.pop(0) if self.answers else None
        return fallback() if answer is None else answer


@pytest.fixture()
def writer(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Writer:
    fake = Writer()
    monkeypatch.setattr(llm, "get_runtime", FakeRuntime)
    monkeypatch.setattr(llm, "invoke_structured", fake.invoke_structured)
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    clear_settings_cache()
    yield fake
    clear_settings_cache()


def harness_input() -> DocInput:
    plan = plan_with()
    return DocInput(
        path=hp.HARNESS_SPEC_PATH,
        strategy=None,
        context_block="Original idea:\nA job list for plumbers.",
        upstream={"ARCHITECTURE.md": "<<arch>>", "plan.md": "<<plan.md>>", "plan.json": plan.model_dump_json()},
        issues="",
        previous="",
    )


def stored(update: dict[str, Any]) -> HarnessSpec:
    return HarnessSpec.model_validate_json(update["blueprint_docs"][hp.HARNESS_SPEC_PATH])


def test_the_harness_writer_reads_the_architecture_and_plan_md_but_not_plan_json(writer: Writer) -> None:
    writer.answers = [spec()]
    assert stored(generate_harness_node(harness_input())) == spec()
    prompt = str(writer.prompts[0][1].content)
    assert "<<arch>>" in prompt and "<<plan.md>>" in prompt and '"contracts"' not in prompt
    assert "`bash scripts/check.sh`" in prompt  # told which scripts exist


def test_an_invalid_harness_gets_one_repair_attempt_with_the_validator_feedback(writer: Writer) -> None:
    writer.answers = [spec(skills=[skill(description="Adds an endpoint.")]), spec()]
    assert stored(generate_harness_node(harness_input())) == spec()
    assert len(writer.prompts) == 2 and "trigger" in str(writer.prompts[1][-1].content)


def test_a_harness_that_stays_invalid_falls_back_to_the_deterministic_one(writer: Writer) -> None:
    bad = spec(skills=[skill(body="Run `scripts/seed.py`.")])
    writer.answers = [bad, bad]
    result = stored(generate_harness_node(harness_input()))
    assert len(writer.prompts) == 2 and result == hp.fallback_harness(plan_with())


def test_a_model_failure_falls_back_without_a_repair_pass(writer: Writer) -> None:
    assert stored(generate_harness_node(harness_input())) == hp.fallback_harness(plan_with())
    assert len(writer.prompts) == 1

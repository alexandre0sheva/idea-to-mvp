"""The CI and tooling files say what Task 22 promises (they are config, so drift is silent otherwise)."""

import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def load_yaml(path: str) -> dict:
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))


def ci_steps() -> list[str]:
    job = load_yaml(".github/workflows/ci.yml")["jobs"]["lint-and-test"]
    return [str(step.get("run", step.get("uses", ""))) for step in job["steps"]]


def test_ci_runs_on_three_python_versions() -> None:
    job = load_yaml(".github/workflows/ci.yml")["jobs"]["lint-and-test"]
    assert job["strategy"]["matrix"]["python-version"] == ["3.11", "3.12", "3.13"]


def test_ci_fails_on_lock_drift_before_installing() -> None:
    steps = ci_steps()
    check = next(i for i, step in enumerate(steps) if "uv lock --check" in step)
    assert check < next(i for i, step in enumerate(steps) if "uv sync" in step)


def test_ci_gates_coverage_at_eighty_percent() -> None:
    (test_step,) = [step for step in ci_steps() if "pytest" in step]
    assert "--cov=idea_to_mvp" in test_step and "--cov-fail-under=80" in test_step


def test_ci_still_lints_and_type_checks() -> None:
    steps = " ".join(ci_steps())
    assert "ruff check" in steps and "mypy" in steps
    # The pre-commit hook alone is skipped by whoever has not installed it.
    assert "ruff format --check" in steps


def test_dependabot_watches_uv_and_github_actions() -> None:
    ecosystems = {
        update["package-ecosystem"] for update in load_yaml(".github/dependabot.yml")["updates"]
    }
    assert ecosystems == {"uv", "github-actions"}


def test_pre_commit_runs_ruff_and_mypy() -> None:
    hooks = {
        hook["id"]
        for repo in load_yaml(".pre-commit-config.yaml")["repos"]
        for hook in repo["hooks"]
    }
    assert {"ruff", "mypy"} <= hooks


def test_the_coverage_gate_is_configured_but_does_not_break_partial_runs() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert config["tool"]["coverage"]["report"]["fail_under"] == 80
    assert config["tool"]["coverage"]["run"]["source"] == ["idea_to_mvp"]
    # coverage is asked for by CI (`--cov`), not by addopts: a single-test run must not fail the 80% gate
    assert "--cov" not in config["tool"]["pytest"]["ini_options"]["addopts"]


def test_the_core_modules_are_type_checked_strictly() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    (override,) = [o for o in config["tool"]["mypy"]["overrides"] if o.get("disallow_untyped_defs")]
    assert {"idea_to_mvp.llm.*", "idea_to_mvp.schemas", "idea_to_mvp.plan"} <= set(
        override["module"]
    )


def test_contributing_documents_live_tests_and_the_eval_script_once() -> None:
    text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "pytest -m live" in text and text.count("scripts/eval_blueprint.py") == 1


def test_ci_checks_every_starter_template_with_the_real_toolchain() -> None:
    job = load_yaml(".github/workflows/ci.yml")["jobs"]["starter-templates"]
    steps = " ".join(str(step.get("run", step.get("uses", ""))) for step in job["steps"])
    assert "pytest -m scaffold" in steps and "actions/setup-node" in steps
    # the main job (with the coverage gate) must not need node or the network for them
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "not scaffold" in config["tool"]["pytest"]["ini_options"]["addopts"]
    assert "scaffold" in " ".join(config["tool"]["pytest"]["ini_options"]["markers"])


# ------------------------------------------------------------------ packaging and release (Task 20)

REPO_URL = "https://github.com/alexandre0sheva/idea-to-mvp"


def project() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def test_the_package_metadata_is_ready_for_pypi() -> None:
    meta = project()
    assert (
        meta["name"] == "idea-to-mvp"
        and meta["license"] == "MIT"
        and meta["license-files"] == ["LICENSE"]
    )
    assert all(author["name"] != "idea-to-mvp contributors" for author in meta["authors"])
    assert {"Homepage", "Repository", "Issues", "Changelog"} <= set(meta["urls"])
    assert all(url.startswith(REPO_URL) for url in meta["urls"].values())
    assert len(meta["keywords"]) >= 5 and len(meta["description"]) > 30
    classifiers = set(meta["classifiers"])
    assert {f"Programming Language :: Python :: 3.{minor}" for minor in (11, 12, 13)} <= classifiers
    # a license expression replaces the License classifier (PEP 639); build tools reject both together
    assert not any(c.startswith("License ::") for c in classifiers)


def test_the_supported_pythons_match_what_ci_tests() -> None:
    job = load_yaml(".github/workflows/ci.yml")["jobs"]["lint-and-test"]
    tested = job["strategy"]["matrix"]["python-version"]
    assert project()["requires-python"] == ">=3.11"
    assert all(
        f"Programming Language :: Python :: {version}" in project()["classifiers"]
        for version in tested
    )


def test_the_release_workflow_builds_and_checks_on_tags_but_never_publishes() -> None:
    workflow = load_yaml(".github/workflows/release.yml")
    triggers = workflow.get("on", workflow.get(True))  # YAML reads a bare `on` as True
    assert triggers == {"push": {"tags": ["v*"]}}
    assert workflow["permissions"] == {"contents": "read"}  # no id-token: publishing stays manual
    steps = [
        str(step.get("run", step.get("uses", "")))
        for job in workflow["jobs"].values()
        for step in job["steps"]
    ]
    text = " ".join(steps)
    assert "uv build" in text and "twine check" in text and "upload-artifact" in text
    assert "GITHUB_REF_NAME" in text and "pyproject.toml" in text  # the tag must match the version
    assert not any(
        word in text for word in ("twine upload", "pypi-publish", "uv publish", "gh release")
    )


def test_pre_commit_formats_with_ruff() -> None:
    hooks = {
        hook["id"]
        for repo in load_yaml(".pre-commit-config.yaml")["repos"]
        for hook in repo["hooks"]
    }
    assert {"ruff", "ruff-format", "mypy"} <= hooks


def test_ruff_format_uses_the_lint_line_length_and_skips_the_template_files() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["ruff"]
    assert config["line-length"] == 100
    assert (
        "src/idea_to_mvp/templates/*/files" in config["extend-exclude"]
    )  # the formatter honours it too


def test_issue_forms_ask_for_a_reproduction_and_warn_about_secrets() -> None:
    config = load_yaml(".github/ISSUE_TEMPLATE/config.yml")
    assert config["blank_issues_enabled"] is False
    assert any("SECURITY" in link["url"] for link in config["contact_links"])
    bug = load_yaml(".github/ISSUE_TEMPLATE/bug_report.yml")
    labels = {field["attributes"]["label"] for field in bug["body"] if field["type"] != "markdown"}
    assert {"What happened", "Steps to reproduce", "Version and setup"} <= labels
    text = (ROOT / ".github/ISSUE_TEMPLATE/bug_report.yml").read_text(encoding="utf-8")
    assert "idea-to-mvp doctor" in text and "API key" in text
    feature = load_yaml(".github/ISSUE_TEMPLATE/feature_request.yml")
    assert feature["name"] and feature["body"]


def test_the_pull_request_template_lists_the_checks_ci_runs() -> None:
    text = (ROOT / ".github/PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")
    for check in (
        "ruff check",
        "ruff format --check",
        "mypy",
        "pytest --cov=idea_to_mvp",
        "CHANGELOG.md",
        "src/idea_to_mvp/demo/",
    ):
        assert check in text


def test_the_readme_has_its_badges_and_the_install_routes() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    head = text.split("## Quick start", 1)[0]
    assert f"{REPO_URL}/actions/workflows/ci.yml" in head
    assert "license-MIT" in head and "python-3.11" in head and "coverage" in head
    assert "uvx idea-to-mvp" in text and "pipx install" in text


def test_contributing_explains_the_release_and_the_old_folders() -> None:
    text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "twine check" in text and "ruff format" in text
    assert (
        "project_blueprints" in text and "generated_projects" in text
    )  # the stale pre-0.3 folders


def test_the_sdist_is_an_allowlist_that_leaves_out_local_files() -> None:
    sdist = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["hatch"][
        "build"
    ]["targets"]["sdist"]
    included = set(sdist["include"])
    assert {"/src", "/tests", "/README.md", "/LICENSE", "/pyproject.toml", "/uv.lock"} <= included
    # `.claude/settings.local.json` and `.env` exist on a developer's machine and must never reach PyPI
    assert not {"/.claude", "/.env"} & included and "/.env.example" in included

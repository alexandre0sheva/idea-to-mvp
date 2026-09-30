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


def test_dependabot_watches_uv_and_github_actions() -> None:
    ecosystems = {update["package-ecosystem"] for update in load_yaml(".github/dependabot.yml")["updates"]}
    assert ecosystems == {"uv", "github-actions"}


def test_pre_commit_runs_ruff_and_mypy() -> None:
    hooks = {hook["id"] for repo in load_yaml(".pre-commit-config.yaml")["repos"] for hook in repo["hooks"]}
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
    assert {"idea_to_mvp.llm.*", "idea_to_mvp.schemas", "idea_to_mvp.plan"} <= set(override["module"])


def test_contributing_documents_live_tests_and_the_eval_script_once() -> None:
    text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "pytest -m live" in text and text.count("scripts/eval_blueprint.py") == 1

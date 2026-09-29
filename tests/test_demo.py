import json
import subprocess
import sys
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from idea_to_mvp import blueprints
from idea_to_mvp.config import Settings
from idea_to_mvp.demo.implementer import demo_implement, demo_verify
from idea_to_mvp.demo.models import DemoChatModel
from idea_to_mvp.roles import ROLES, SUMMARY_SYSTEM
from idea_to_mvp.schemas import ArchitectureProposal, ExecutionStrategy, QuestionSet

IDEA = "A habit tracker for climbing gyms"


def _ask(role: str, system: str, human: str) -> str:
    model = DemoChatModel(role=role)
    reply = model.invoke([SystemMessage(content=system), HumanMessage(content=human)])
    return str(reply.content)


@pytest.mark.parametrize("role", ["pm", "tech_lead", "skeptic"])
def test_panelists_echo_the_idea(role: str) -> None:
    text = _ask(role, ROLES[role].system_prompt, f"Anchor idea:\n{IDEA}\n\nPanel transcript so far:\n")
    assert "climbing gyms" in text


def test_summarizer_produces_the_brief() -> None:
    summary = _ask("summarizer", SUMMARY_SYSTEM, f"Original idea:\n{IDEA}\n\nFull discussion thread")
    assert "## Executive summary" in summary and "climbing gyms" in summary


@pytest.mark.parametrize(
    ("schema", "check"),
    [
        (QuestionSet, lambda r: len(r.questions) == 5 and all(q.suggested_answer for q in r.questions)),
        (ArchitectureProposal, lambda r: r.option_a.key == "A" and r.option_b.key == "B"),
        (ExecutionStrategy, lambda r: len(r.workstreams) >= 2 and r.mode in ("subagents", "agent_team")),
    ],
)
def test_structured_outputs_come_from_fixtures_and_validate(schema, check) -> None:
    model = DemoChatModel(role="architect")
    result = model.with_structured_output(schema).invoke(
        [SystemMessage(content="s"), HumanMessage(content=f"Idea:\n{IDEA}\n\nmore")], max_output_tokens=10
    )
    assert isinstance(result, schema) and check(result)


def test_unknown_schema_or_architect_prompt_fails_loudly() -> None:
    from pydantic import BaseModel

    class Unknown(BaseModel):
        x: int

    with pytest.raises(KeyError, match="Unknown"):
        DemoChatModel(role="architect").with_structured_output(Unknown).invoke([HumanMessage(content="x")])
    with pytest.raises(KeyError, match="demo fixture"):
        _ask("architect", "a prompt no fixture knows", "x")


def test_every_blueprint_document_gets_distinct_demo_content() -> None:
    strategy = {"mode": "agent_team", "workstreams": [{"name": "core-app", "focus": "f", "deliverables": "d"}]}
    context = (
        f"Original idea:\n{IDEA}\n\nExecution strategy:\n{json.dumps(strategy)}\n"
    )
    seen: dict[str, str] = {}
    for spec in blueprints.bundle_file_plan(strategy):
        text = _ask("architect", spec.system_prompt, f"{context}\n\n{spec.instruction}")
        assert text.strip(), spec.relative_path
        assert text != spec.fallback, f"{spec.relative_path} fell through to the generic fallback"
        seen[spec.relative_path] = text
    assert len(set(seen.values())) == len(seen)
    assert "core-app" in seen["plan.md"]
    assert "R1" in seen["PRD.md"]


def test_demo_implementation_builds_a_project_whose_tests_pass(tmp_path: Path) -> None:
    workspace = tmp_path / "20260101-000000-000000-a-habit-tracker-for-climbing-gyms"
    workspace.mkdir()
    strategy = {"mode": "agent_team", "workstreams": [{"name": "core-app"}, {"name": "quality"}]}
    summary = demo_implement(workspace, strategy)
    assert "core-app" in summary and "quality" in summary
    assert (workspace / "README.md").exists()
    result = demo_verify(workspace)
    assert result["passed"] is True
    assert "VERDICT: PASS" in result["report"]
    # The generated project really runs from a clean interpreter.
    run = subprocess.run(
        [sys.executable, "-m", "demo_app"], cwd=workspace, capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0 and run.stdout.strip()


def test_demo_verify_reports_failure_when_tests_fail(tmp_path: Path) -> None:
    demo_implement(tmp_path, {"workstreams": []})
    (tmp_path / "tests" / "test_broken.py").write_text(
        "import unittest\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        self.assertTrue(False)\n"
    )
    result = demo_verify(tmp_path)
    assert result["passed"] is False
    assert "VERDICT: FAIL" in result["report"]


def test_demo_mode_setting_defaults_off() -> None:
    assert Settings(_env_file=None).demo_mode is False

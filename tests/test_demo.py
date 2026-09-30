import json
import subprocess
import sys
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from idea_to_mvp import blueprints
from idea_to_mvp.blueprint_review import CritiqueReport
from idea_to_mvp.config import Settings
from idea_to_mvp.demo.implementer import demo_implement, demo_verify_lane
from idea_to_mvp.demo.models import DemoChatModel
from idea_to_mvp.plan import Plan, execution_waves, validate_plan
from idea_to_mvp.roles import ROLES, SUMMARY_SYSTEM
from idea_to_mvp.schemas import (
    ArchitectureProposal,
    ExecutionStrategy,
    ModeratorDecision,
    QuestionSet,
)

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
        if spec.structured:  # the plan comes from the structured Plan fixture (tested below)
            continue
        text = _ask("architect", spec.system_prompt, f"{context}\n\n{spec.instruction}")
        assert text.strip(), spec.relative_path
        assert text != spec.fallback, f"{spec.relative_path} fell through to the generic fallback"
        seen[spec.relative_path] = text
    assert len(set(seen.values())) == len(seen)
    assert "R1" in seen["PRD.md"]


def test_demo_implementation_builds_a_project_whose_tests_pass(tmp_path: Path) -> None:
    workspace = tmp_path / "20260101-000000-000000-a-habit-tracker-for-climbing-gyms"
    workspace.mkdir()
    strategy = {"mode": "agent_team", "workstreams": [{"name": "core-app"}, {"name": "quality"}]}
    summary = demo_implement(workspace, strategy)
    assert "core-app" in summary and "quality" in summary
    assert (workspace / "README.md").exists()
    result = demo_verify_lane(workspace, "tests")
    assert result.passed is True
    assert "Ran 3 tests" in result.summary
    # The generated project really runs from a clean interpreter.
    run = subprocess.run(
        [sys.executable, "-m", "demo_app"], cwd=workspace, capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0 and run.stdout.strip()


def test_demo_tests_lane_reports_failure_when_tests_fail(tmp_path: Path) -> None:
    demo_implement(tmp_path, {"workstreams": []})
    (tmp_path / "tests" / "test_broken.py").write_text(
        "import unittest\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        self.assertTrue(False)\n"
    )
    result = demo_verify_lane(tmp_path, "tests")
    assert result.passed is False
    assert result.failures and result.commands_run[0]["exit_code"] != 0


def test_demo_mode_setting_defaults_off() -> None:
    assert Settings(_env_file=None).demo_mode is False


def _moderate(turns: dict[str, int]) -> ModeratorDecision:
    counts = ", ".join(f"{name}: {count}" for name, count in turns.items())
    model = DemoChatModel(role="moderator")
    return model.with_structured_output(ModeratorDecision).invoke(  # type: ignore[return-value]
        [SystemMessage(content="s"), HumanMessage(content=f"Anchor idea:\n{IDEA}\n\nTurns so far: {counts}.\n")]
    )


def test_demo_moderator_steers_to_the_quietest_speaker_then_converges() -> None:
    decision = _moderate({"PM": 2, "Tech Lead": 1, "Skeptic": 2})
    assert not decision.converged and decision.next_speaker == "Tech Lead"
    done = _moderate({"PM": 2, "Tech Lead": 2, "Skeptic": 2})
    assert done.converged and done.next_speaker is None and done.reason


def _structured(schema, human: str):
    model = DemoChatModel(role="plan_writer")
    return model.with_structured_output(schema).invoke([SystemMessage(content="s"), HumanMessage(content=human)])


def _plan_prompt(prd: str, strategy: dict) -> str:
    plan_spec = next(s for s in blueprints.bundle_file_plan(strategy) if s.structured)
    context = f"Idea:\n{IDEA}\n\nExecution strategy:\n{json.dumps(strategy, indent=2)}"
    return blueprints.prompt_for(plan_spec, context, {"PRD.md": prd})


def test_demo_plan_is_a_valid_dag_over_the_real_workstreams_and_prd_requirements() -> None:
    strategy = {"mode": "agent_team", "workstreams": [{"name": "core-app"}, {"name": "quality"}]}
    prd = "## Requirements\n- R7 (P0): log a climb\n- R9 (P0): export\n- R12 (P1): share\n"
    plan = _structured(Plan, _plan_prompt(prd, strategy))
    assert validate_plan(plan, prd_markdown=prd, workstreams=["core-app", "quality"]) == []
    assert {t.workstream for t in plan.tasks} == {"core-app", "quality"}
    assert {r for t in plan.tasks for r in t.requirement_ids} == {"R7", "R9", "R12"}
    assert len(execution_waves(plan)) >= 2


def test_demo_plan_without_a_prd_or_strategy_still_validates() -> None:
    plan = _structured(Plan, f"Idea:\n{IDEA}\n\nno strategy here")
    assert validate_plan(plan, prd_markdown="- R1 (P0): a\n- R2 (P0): b\n- R3 (P1): c\n", workstreams=["core-product"]) == []


def test_demo_critic_approves_the_pack() -> None:
    report = _structured(CritiqueReport, f"Idea:\n{IDEA}")
    assert report.approved and report.issues == []

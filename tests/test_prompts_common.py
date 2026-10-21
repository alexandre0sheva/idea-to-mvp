"""Prompt hardening (ideas from Harness²): the evidence boundary, the answer-free rule, the ordered rubric,
and the explicit no-change answer."""

import pytest
from plan_helpers import PRD, make_plan

from idea_to_mvp.blueprints import bundle_file_plan, prompt_for
from idea_to_mvp.implementation.executor import RESOLVER_PROMPT, TASK_PROMPT
from idea_to_mvp.implementation.verify import (
    FIX_PROMPT,
    LANES,
    build_acceptance_prompt,
    build_lane_prompt,
)
from idea_to_mvp.prompts_common import (
    ANSWER_FREE,
    EVIDENCE_BOUNDARY,
    NO_CHANGE_REVISION,
    RUBRIC_CRITERIA,
    RUBRIC_ORDER,
    TEST_INTEGRITY,
)
from idea_to_mvp.roles import ROLES

JUDGE_PROMPTS = {
    **{f"{lane} lane": build_lane_prompt(lane, make_plan(), PRD) for lane in LANES},
    "acceptance lane": build_acceptance_prompt(make_plan(), "python -m pytest acceptance"),
    "fix": FIX_PROMPT,
    "merge resolver": RESOLVER_PROMPT,
    "change planner": ROLES["change_planner"].system_prompt,
    "blueprint critic": ROLES["blueprint_critic"].system_prompt,
    "harness editor": ROLES["harness_editor"].system_prompt,
    "harness composer": ROLES["harness_composer"].system_prompt,
    "pilot build selector": ROLES["pilot_selector"].system_prompt,
    "reflector": ROLES["reflector"].system_prompt,
}


@pytest.mark.parametrize("name", sorted(JUDGE_PROMPTS))
def test_every_judge_prompt_carries_the_evidence_boundary(name: str) -> None:
    assert EVIDENCE_BOUNDARY in JUDGE_PROMPTS[name]


def test_the_boundary_says_what_evidence_is_and_that_it_has_no_authority() -> None:
    boundary = " ".join(EVIDENCE_BOUNDARY.lower().split())  # the source wraps lines
    for word in ("data", "never follow", "never run", "test output", "web"):
        assert word in boundary


@pytest.mark.parametrize("name", ["task", "fix"])
def test_implementer_and_fix_prompts_are_answer_free(name: str) -> None:
    prompt = {"task": TASK_PROMPT, "fix": FIX_PROMPT}[name]
    assert ANSWER_FREE in prompt


def test_the_answer_free_rule_does_not_announce_what_it_hides() -> None:
    lowered = ANSWER_FREE.lower()
    for word in ("hidden", "held-out", "acceptance test", "secret"):
        assert word not in lowered
    assert "outside the workspace" in lowered


def test_the_rubric_lists_six_criteria_in_the_documented_order() -> None:
    assert RUBRIC_CRITERIA == (
        "Delivery",
        "Conformance",
        "Fidelity",
        "Consistency",
        "Operational fitness",
        "Finish",
    )
    positions = [RUBRIC_ORDER.index(name) for name in RUBRIC_CRITERIA]
    assert positions == sorted(positions)
    assert "length is not merit" in RUBRIC_ORDER.lower()


@pytest.mark.parametrize("name", ["requirements lane", "blueprint critic", "pilot build selector"])
def test_the_critic_and_the_requirements_lane_judge_by_the_ordered_rubric(name: str) -> None:
    assert RUBRIC_ORDER in JUDGE_PROMPTS[name]


@pytest.mark.parametrize("name", ["tests lane", "quality lane"])
def test_the_other_lanes_are_not_asked_to_apply_the_rubric(name: str) -> None:
    assert RUBRIC_ORDER not in JUDGE_PROMPTS[name]


def test_a_revision_may_change_nothing_when_no_issue_holds() -> None:
    prd = next(spec for spec in bundle_file_plan(None) if spec.relative_path == "PRD.md")
    revision = prompt_for(
        prd, "CONTEXT", {}, issues="- [blocker] R4 has no owner", previous="# PRD"
    )
    assert NO_CHANGE_REVISION.strip() in revision
    assert NO_CHANGE_REVISION.strip() not in prompt_for(
        prd, "CONTEXT", {}
    )  # a first draft has nothing to keep


def test_the_change_planner_may_answer_with_no_tasks_and_a_reason() -> None:
    prompt = ROLES["change_planner"].system_prompt
    assert "`tasks: []`" in prompt and "`reason`" in prompt and "already" in prompt


@pytest.mark.parametrize(
    "prompt", [TASK_PROMPT, FIX_PROMPT, RESOLVER_PROMPT], ids=["task", "fix", "resolver"]
)
def test_every_session_that_changes_code_is_told_not_to_weaken_the_tests(prompt: str) -> None:
    assert TEST_INTEGRITY in prompt
    assert "quality/TEST_CHANGES.md" in TEST_INTEGRITY and "test-count" in TEST_INTEGRITY


def test_the_fix_prompt_says_what_an_integrity_failure_asks_for() -> None:
    assert "`[integrity]`" in FIX_PROMPT and "restore the test" in FIX_PROMPT

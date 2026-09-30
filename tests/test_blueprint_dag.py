"""The blueprint pack as a subgraph: dependency waves, a structured plan, a critic and a revise loop."""

import json
import threading
from collections.abc import Callable
from typing import Any

import pytest
from langchain_core.messages import BaseMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from plan_helpers import make_plan, make_task

from idea_to_mvp import llm
from idea_to_mvp.blueprint_review import CritiqueReport, Issue
from idea_to_mvp.blueprints import SUBAGENT_SYSTEM, bundle_file_plan
from idea_to_mvp.config import clear_settings_cache
from idea_to_mvp.demo.fixtures import respond_structured
from idea_to_mvp.graph import build_graph, pending_interrupt, run_config
from idea_to_mvp.nodes.blueprint_graph import build_blueprint_subgraph
from idea_to_mvp.plan import Plan, validate_plan
from idea_to_mvp.state import make_initial_state

STRATEGY = {
    "mode": "agent_team",
    "reasoning": "Two loosely coupled workstreams.",
    "workstreams": [
        {"name": "backend-api", "focus": "Build the API", "deliverables": "REST API"},
        {"name": "web-ui", "focus": "Build the UI", "deliverables": "Frontend"},
    ],
}
WAVE_OF_PROMPT = {spec.system_prompt: spec.wave for spec in bundle_file_plan(STRATEGY)}
WAVE_SIZES = {1: 2, 2: 6, 3: 2}  # wave 2 = README, 4 AGENTS guides, and the plan writer
PRD_TEXT = "<<generated PRD.md>>\n- R1 (P0): core\n- R2 (P1): extra\n"
APPROVED = CritiqueReport(approved=True, issues=[])


def blocker(file: str, text: str = "contradiction") -> Issue:
    return Issue(severity="blocker", file=file, description=text)


def warning(file: str, text: str = "minor") -> Issue:
    return Issue(severity="warning", file=file, description=text)


def good_plan() -> Plan:
    return make_plan(
        [
            make_task("T01", requirement_ids=["R1"]),
            make_task("T02", workstream="web-ui", depends_on=["T01"], requirement_ids=["R1"]),
        ]
    )


def broken_plan() -> Plan:
    return make_plan([make_task("T01", workstream="mobile", requirement_ids=["R1"])])  # unknown workstream


class FakeRuntime:
    def __init__(self, role: str) -> None:
        self.role = role
        self.llm = object()
        self.provider = "anthropic"
        self.model = "fake-model"
        self.max_tokens = 256
        self.system_prompt = f"system::{role}"


class Recorder:
    """Fake model layer: labels every document, records prompts, timing, and plan/critic calls."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.prompts: dict[str, list[str]] = {}  # document label -> every prompt it was generated from
        self.events: list[tuple[str, int]] = []  # ("start" | "end", wave)
        self.barriers: dict[int, threading.Barrier] = {}
        self.empty: set[str] = set()
        self.fail_once: set[str] = set()
        self.delay = 0.0
        self.max_active = 0
        self._active = 0
        self.plans: list[Plan] = []  # scripted planner answers, then good_plan()
        self.plan_calls = 0
        self.plan_fails = False  # simulate a planner that never produces valid output (fallback is used)
        self.critic: Callable[[int], CritiqueReport] = lambda _n: APPROVED
        self.critic_calls = 0
        self.critic_prompts: list[str] = []
        self.critic_fails = False

    def first(self, label: str) -> str:
        return self.prompts[label][0]

    def count(self, label: str) -> int:
        return len(self.prompts.get(label, []))

    def _enter(self, label: str, wave: int, prompt: str) -> None:
        with self.lock:
            self.prompts.setdefault(label, []).append(prompt)
            self.events.append(("start", wave))
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        if self.delay:
            threading.Event().wait(self.delay)
        if wave in self.barriers:
            self.barriers[wave].wait(timeout=5)  # only passes if the whole wave is in flight at once
        if label in self.fail_once:
            self.fail_once.discard(label)
            raise RuntimeError("model exploded")  # not transient: no automatic retry

    def _exit(self, wave: int) -> None:
        with self.lock:
            self._active -= 1
            self.events.append(("end", wave))

    def label_of(self, system: str, prompt: str) -> str:
        by_prompt = {s.system_prompt: s.relative_path for s in bundle_file_plan(STRATEGY) if s.wave < 3}
        if system in by_prompt:
            return by_prompt[system]
        assert system == SUBAGENT_SYSTEM
        # the instruction comes last, after any upstream documents and issue sections
        tail = prompt.rsplit("Write the subagent definition body for workstream", 1)[1]
        return next(f".claude/agents/{w['name']}.md" for w in STRATEGY["workstreams"] if f"`{w['name']}`" in tail)

    def invoke_text(self, runtime: Any, messages: list[BaseMessage], *, max_tokens: int | None = None) -> str:
        system, prompt = str(messages[0].content), str(messages[1].content)
        wave = WAVE_OF_PROMPT[system]
        label = self.label_of(system, prompt)
        self._enter(label, wave, prompt)
        try:
            if label in self.empty:
                return ""
            return PRD_TEXT if label == "PRD.md" else f"<<generated {label}>>"
        finally:
            self._exit(wave)

    def invoke_structured(self, runtime: Any, messages: list[BaseMessage], schema: Any, *, fallback: Any = None) -> Any:
        prompt = "\n".join(str(m.content) for m in messages)
        if schema is Plan:
            with self.lock:
                self.plan_calls += 1
                scripted = self.plans.pop(0) if self.plans else good_plan()
            self._enter("plan.md", 2, prompt)
            try:
                return fallback() if self.plan_fails else scripted
            finally:
                self._exit(2)
        if schema is CritiqueReport:
            with self.lock:
                self.critic_calls += 1
                call = self.critic_calls
                self.critic_prompts.append(prompt)
            return fallback() if self.critic_fails else self.critic(call)
        return respond_structured(schema, messages)


@pytest.fixture()
def rec(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Recorder:
    recorder = Recorder()
    monkeypatch.setattr(llm, "get_runtime", FakeRuntime)
    monkeypatch.setattr(llm, "invoke_text", recorder.invoke_text)
    monkeypatch.setattr(llm, "invoke_structured", recorder.invoke_structured)
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))
    monkeypatch.delenv("MAX_BLUEPRINT_REVISIONS", raising=False)
    clear_settings_cache()
    yield recorder
    clear_settings_cache()


def max_revisions(monkeypatch: pytest.MonkeyPatch, value: int) -> None:
    monkeypatch.setenv("MAX_BLUEPRINT_REVISIONS", str(value))
    clear_settings_cache()


def blueprint_state(strategy: dict[str, Any] | None = None) -> dict[str, Any]:
    state: dict[str, Any] = dict(make_initial_state("A todo app for plumbers", 1))
    state.update(
        summary="Plumbers need a simple job list.",
        generated_questions=["1. Who is the user?"],
        user_answers="Solo plumbers.",
        architecture="Option A: monolith. Option B: services.",
        arch_choice={"option": "A", "notes": ""},
        execution_strategy=STRATEGY if strategy is None else strategy,
        plan_decision={"generate": True, "notes": "keep it lean"},
    )
    return state


def run_blueprint(state: dict[str, Any] | None = None, config: Any = None) -> dict[str, Any]:
    return build_blueprint_subgraph().invoke(state or blueprint_state(), config=config)


def bundle_of(tmp_path):
    (bundle_dir,) = list((tmp_path / "blueprints").iterdir())
    return bundle_dir


# ------------------------------------------------------------ what each doc sees


def test_the_plan_is_written_from_the_generated_prd_and_architecture(rec: Recorder) -> None:
    run_blueprint()
    plan_prompt = rec.first("plan.md")
    assert "<<generated PRD.md>>" in plan_prompt and "<<generated ARCHITECTURE.md>>" in plan_prompt
    assert "Upstream document: PRD.md" in plan_prompt
    assert "Plumbers need a simple job list." in plan_prompt  # the shared context block is still there


def test_the_first_wave_sees_no_upstream_documents(rec: Recorder) -> None:
    run_blueprint()
    for path in ("PRD.md", "ARCHITECTURE.md"):
        assert "Upstream document" not in rec.first(path)


def test_subagent_definitions_are_written_from_the_rendered_plan(rec: Recorder) -> None:
    run_blueprint()
    for name in ("backend-api", "web-ui"):
        prompt = rec.first(f".claude/agents/{name}.md")
        assert "Upstream document: plan.md" in prompt and "### T02. Task T02" in prompt
        assert f"`{name}`" in prompt


def test_an_empty_generation_is_replaced_by_its_fallback_for_downstream_documents(rec: Recorder) -> None:
    rec.empty = {"PRD.md"}
    result = run_blueprint()
    fallback = {s.relative_path: s for s in bundle_file_plan(STRATEGY)}["PRD.md"].fallback.strip()
    assert fallback in rec.first("plan.md")  # the plan sees what will actually be written
    assert result["blueprint_docs"]["PRD.md"] == fallback


# ------------------------------------------------------------- waves and timing


def test_each_wave_requests_all_of_its_documents_concurrently(rec: Recorder) -> None:
    rec.barriers = {wave: threading.Barrier(size, timeout=5) for wave, size in WAVE_SIZES.items()}
    result = run_blueprint()  # a sequential implementation would time the barriers out
    assert all(not barrier.broken for barrier in rec.barriers.values())
    assert {"plan.json", "plan.md"} <= set(result["blueprint_docs"]) and len(result["blueprint_docs"]) == 11


def test_a_wave_starts_only_after_the_previous_wave_finished(rec: Recorder) -> None:
    run_blueprint()
    waves_in_start_order = [wave for kind, wave in rec.events if kind == "start"]
    assert waves_in_start_order == sorted(waves_in_start_order)
    last_end = {wave: max(i for i, e in enumerate(rec.events) if e == ("end", wave)) for wave in (1, 2)}
    first_start = {wave: min(i for i, e in enumerate(rec.events) if e == ("start", wave)) for wave in (2, 3)}
    assert last_end[1] < first_start[2] and last_end[2] < first_start[3]


@pytest.mark.parametrize(("limit", "expected_peak"), [(2, 2), (8, 6)])  # the widest wave has 6 documents
def test_run_config_caps_parallel_generation(rec: Recorder, limit: int, expected_peak: int) -> None:
    rec.delay = 0.05
    run_blueprint(config=run_config("cap", max_concurrency=limit))
    assert rec.max_active == expected_peak


def test_without_workstreams_the_third_wave_is_skipped_and_the_bundle_is_still_written(rec: Recorder) -> None:
    empty_strategy = {"mode": "", "reasoning": "", "workstreams": []}
    rec.plans = [make_plan([make_task("T01", workstream="core-product", requirement_ids=["R1"])])]
    result = run_blueprint(blueprint_state(empty_strategy))
    assert sorted(rec.prompts) == sorted(s.relative_path for s in bundle_file_plan(empty_strategy))
    assert result["project_bundle_files"][-1] == "STRATEGY.json" and len(result["project_bundle_files"]) == 10


# ------------------------------------------------------------------ the plan


def test_the_bundle_holds_plan_json_as_source_of_truth_and_the_rendered_plan_md(rec: Recorder, tmp_path) -> None:
    run_blueprint()
    bundle_dir = bundle_of(tmp_path)
    plan = Plan.model_validate_json((bundle_dir / "plan.json").read_text())
    assert [t.id for t in plan.tasks] == ["T01", "T02"]
    rendered = (bundle_dir / "plan.md").read_text()
    assert "## Contract Registry" in rendered and "### T02. Task T02" in rendered
    assert validate_plan(plan, prd_markdown=PRD_TEXT, workstreams=["backend-api", "web-ui"]) == []


def test_an_invalid_plan_gets_one_repair_attempt_with_the_validator_feedback(rec: Recorder, tmp_path) -> None:
    rec.plans = [broken_plan(), good_plan()]
    run_blueprint()
    assert rec.plan_calls == 2
    repair_prompt = rec.prompts["plan.md"][1]
    assert "unknown workstream" in repair_prompt and "mobile" in repair_prompt
    plan = Plan.model_validate_json((bundle_of(tmp_path) / "plan.json").read_text())
    assert {t.workstream for t in plan.tasks} == {"backend-api", "web-ui"}  # the repaired plan was kept


def test_a_repair_that_makes_things_worse_is_discarded(rec: Recorder, tmp_path) -> None:
    worse = make_plan([make_task("T01", workstream="mobile", depends_on=["T09"], requirement_ids=["R9"])])
    rec.plans = [broken_plan(), worse]
    run_blueprint()
    plan = Plan.model_validate_json((bundle_of(tmp_path) / "plan.json").read_text())
    assert plan.tasks[0].depends_on == []  # the first, less broken, attempt


def test_a_planner_that_cannot_produce_valid_output_falls_back_to_a_valid_plan(rec: Recorder, tmp_path) -> None:
    rec.plan_fails = True
    run_blueprint()
    plan = Plan.model_validate_json((bundle_of(tmp_path) / "plan.json").read_text())
    assert validate_plan(plan, prd_markdown=PRD_TEXT, workstreams=["backend-api", "web-ui"]) == []
    assert rec.plan_calls == 1  # a valid fallback needs no repair pass


# --------------------------------------------------------------- critic + revise


def test_the_critic_reads_the_prd_architecture_plan_and_subagent_prompts(rec: Recorder) -> None:
    run_blueprint()
    assert rec.critic_calls == 1
    prompt = rec.critic_prompts[0]
    for needle in ("<<generated PRD.md>>", "<<generated ARCHITECTURE.md>>", "### T02. Task T02"):
        assert needle in prompt
    assert "<<generated .claude/agents/web-ui.md>>" in prompt


def test_an_approved_pack_has_no_review_file_and_a_single_critic_pass(rec: Recorder, tmp_path) -> None:
    result = run_blueprint()
    assert rec.critic_calls == 1 and "REVIEW.md" not in result["project_bundle_files"]
    assert not (bundle_of(tmp_path) / "REVIEW.md").exists()
    assert result["blueprint_review"]["approved"] is True and result["blueprint_review"]["revisions"] == 0


def test_warnings_do_not_trigger_a_revision_but_end_up_in_review_md(rec: Recorder, tmp_path) -> None:
    rec.critic = lambda _n: CritiqueReport(approved=True, issues=[warning("PRD.md", "vague success metric")])
    result = run_blueprint()
    assert rec.count("PRD.md") == 1 and rec.critic_calls == 1
    assert "vague success metric" in (bundle_of(tmp_path) / "REVIEW.md").read_text()
    assert "REVIEW.md" in result["project_bundle_files"]
    assert "1 open review note" in result["project_bundle_summary"]


def test_blockers_regenerate_only_the_named_documents_then_the_pack_is_reviewed_again(
    rec: Recorder, tmp_path
) -> None:
    rec.critic = lambda n: (
        CritiqueReport(approved=False, issues=[blocker("PRD.md", "R2 is never testable")]) if n == 1 else APPROVED
    )
    result = run_blueprint()
    assert rec.count("PRD.md") == 2
    revision_prompt = rec.prompts["PRD.md"][1]
    assert "R2 is never testable" in revision_prompt and "Your previous version of PRD.md" in revision_prompt
    assert PRD_TEXT.strip() in revision_prompt  # the model revises its own text
    for untouched in ("ARCHITECTURE.md", "README.md", "AGENTS.md", ".claude/agents/web-ui.md"):
        assert rec.count(untouched) == 1, untouched
    assert rec.plan_calls == 1
    assert rec.critic_calls == 2  # re-reviewed after the revision
    assert result["blueprint_review"]["revisions"] == 1 and result["blueprint_review"]["approved"] is True
    assert not (bundle_of(tmp_path) / "REVIEW.md").exists()


def test_revisions_are_capped_and_unresolved_blockers_are_flagged(rec: Recorder, tmp_path) -> None:
    rec.critic = lambda _n: CritiqueReport(approved=False, issues=[blocker("ARCHITECTURE.md", "no auth story")])
    result = run_blueprint()  # MAX_BLUEPRINT_REVISIONS defaults to 1
    assert rec.count("ARCHITECTURE.md") == 2 and rec.critic_calls == 2
    review = (bundle_of(tmp_path) / "REVIEW.md").read_text()
    assert "no auth story" in review and "blocker" in review.lower()
    assert result["blueprint_review"]["approved"] is False and result["blueprint_review"]["revisions"] == 1


def test_zero_revisions_means_the_critic_only_reports(rec: Recorder, monkeypatch, tmp_path) -> None:
    max_revisions(monkeypatch, 0)
    rec.critic = lambda _n: CritiqueReport(approved=False, issues=[blocker("PRD.md", "thin")])
    run_blueprint()
    assert rec.count("PRD.md") == 1 and rec.critic_calls == 1
    assert "thin" in (bundle_of(tmp_path) / "REVIEW.md").read_text()


def test_two_revisions_allow_a_second_round(rec: Recorder, monkeypatch) -> None:
    max_revisions(monkeypatch, 2)
    rec.critic = lambda n: (
        CritiqueReport(approved=False, issues=[blocker("PRD.md", f"round {n}")]) if n < 3 else APPROVED
    )
    result = run_blueprint()
    assert rec.count("PRD.md") == 3 and rec.critic_calls == 3
    assert result["blueprint_review"]["revisions"] == 2


def test_a_critic_that_approves_but_lists_a_blocker_is_not_believed(rec: Recorder) -> None:
    rec.critic = lambda n: (
        CritiqueReport(approved=True, issues=[blocker("PRD.md", "contradiction")]) if n == 1 else APPROVED
    )
    run_blueprint()
    assert rec.count("PRD.md") == 2  # the blocker still triggered a revision


def test_a_blocker_on_the_plan_reruns_the_planner_with_the_issues(rec: Recorder, tmp_path) -> None:
    rec.critic = lambda n: (
        CritiqueReport(approved=False, issues=[blocker("plan.json", "T02 should precede T01")]) if n == 1 else APPROVED
    )
    run_blueprint()
    assert rec.plan_calls == 2
    revision_prompt = rec.prompts["plan.md"][1]
    assert "T02 should precede T01" in revision_prompt and '"id": "T01"' in revision_prompt  # previous plan.json


def test_blockers_that_name_no_regenerable_file_are_reported_not_revised(rec: Recorder, tmp_path) -> None:
    rec.critic = lambda _n: CritiqueReport(approved=False, issues=[blocker("the whole pack", "stack mismatch")])
    run_blueprint()
    assert rec.critic_calls == 1 and all(rec.count(label) == 1 for label in rec.prompts)
    assert "stack mismatch" in (bundle_of(tmp_path) / "REVIEW.md").read_text()


def test_plan_problems_that_survive_the_repair_are_blockers_the_revision_loop_retries(rec: Recorder, tmp_path) -> None:
    rec.plans = [broken_plan(), broken_plan(), broken_plan(), broken_plan()]  # initial+repair, then revision+repair
    result = run_blueprint()
    assert rec.plan_calls == 4 and rec.critic_calls == 2
    review = (bundle_of(tmp_path) / "REVIEW.md").read_text()
    assert "unknown workstream" in review and "plan.md" in review
    assert result["blueprint_review"]["approved"] is False


def test_an_unavailable_critic_is_flagged_as_a_warning_and_never_blocks(rec: Recorder, tmp_path) -> None:
    rec.critic_fails = True
    result = run_blueprint()
    assert rec.critic_calls == 1 and result["stage"] == "plan_bundle"
    assert "could not be completed" in (bundle_of(tmp_path) / "REVIEW.md").read_text()


# ------------------------------------------------------------------- the result


def test_the_bundle_is_written_from_the_generated_documents(rec: Recorder, tmp_path) -> None:
    result = run_blueprint()
    bundle_dir = bundle_of(tmp_path)
    assert result["project_bundle_dir"] == str(bundle_dir)
    assert (bundle_dir / "PRD.md").read_text().strip() == PRD_TEXT.strip()
    assert "<<generated .claude/agents/web-ui.md>>" in (bundle_dir / ".claude/agents/web-ui.md").read_text()
    assert json.loads((bundle_dir / "STRATEGY.json").read_text())["mode"] == "agent_team"
    assert result["project_bundle_files"][:2] == ["README.md", "PRD.md"]
    assert "plan.json" in result["project_bundle_files"]
    assert str(bundle_dir) in result["project_bundle_summary"] and result["stage"] == "plan_bundle"


def test_the_blueprint_is_one_parent_node_whose_llm_nodes_carry_the_retry_policy() -> None:
    parent = build_graph().builder.nodes
    assert not parent["plan_bundle"].retry_policy  # the inner nodes retry instead
    inner = build_blueprint_subgraph().builder.nodes
    for name in ("doc_1", "doc_2", "doc_3", "plan_writer", "review", "rewrite_doc", "rewrite_plan"):
        assert inner[name].retry_policy, name
    for name in ("write", "revise", "wave_1"):
        assert not inner[name].retry_policy, name


def test_blueprint_usage_is_recorded_once_in_the_parent_state(demo_env) -> None:
    sentinel = {"role": "earlier", "provider": "", "model": "m", "input_tokens": 1, "output_tokens": 1, "cost_usd": None}
    state = blueprint_state()
    state["usage"] = [sentinel]
    graph = build_graph(MemorySaver())
    config = run_config("bp-usage")
    # Drive the parent graph straight into the blueprint step from a saved checkpoint.
    graph.update_state(config, state, as_node="strategy")
    graph.invoke(None, config)  # plan_gate interrupt
    graph.invoke(Command(resume={"generate": True, "notes": ""}), config)
    usage = graph.get_state(config).values["usage"]
    assert usage.count(sentinel) == 1
    assert len([r for r in usage if r["role"] == "architect"]) == 9  # one per text document, none doubled


def test_a_crash_in_a_later_wave_resumes_without_redoing_finished_waves(rec: Recorder, tmp_path) -> None:
    graph = build_graph(MemorySaver())
    config = run_config("bp-crash")
    graph.update_state(config, blueprint_state(), as_node="strategy")
    graph.invoke(None, config)  # pauses at the plan gate

    rec.fail_once = {"plan.md"}
    with pytest.raises(RuntimeError, match="exploded"):
        graph.invoke(Command(resume={"generate": True, "notes": ""}), config)
    assert rec.events.count(("start", 1)) == 2 and not list((tmp_path / "blueprints").glob("*"))

    graph.invoke(None, config)
    assert rec.events.count(("start", 1)) == 2  # the PRD and ARCHITECTURE were not written again
    assert rec.events.count(("start", 3)) == 2
    values = graph.get_state(config).values
    assert values["project_bundle_files"][-1] == "STRATEGY.json"
    assert "### T01." in (bundle_of(tmp_path) / "plan.md").read_text()


def test_the_implement_gate_shows_the_open_review_notes(rec: Recorder) -> None:
    rec.critic = lambda _n: CritiqueReport(approved=True, issues=[warning("PRD.md", "vague success metric")])
    graph = build_graph(MemorySaver())
    config = run_config("bp-gate")
    graph.update_state(config, blueprint_state(), as_node="strategy")
    graph.invoke(None, config)
    graph.invoke(Command(resume={"generate": True, "notes": ""}), config)
    gate = pending_interrupt(graph.get_state(config))
    assert gate and gate["kind"] == "implement_gate"
    assert "1 open review note" in gate["question"] and "REVIEW.md" in gate["question"]
    assert gate["review_issues"] == ["vague success metric"]


def test_the_implement_gate_is_unchanged_when_the_pack_is_clean(rec: Recorder) -> None:
    graph = build_graph(MemorySaver())
    config = run_config("bp-gate-clean")
    graph.update_state(config, blueprint_state(), as_node="strategy")
    graph.invoke(None, config)
    graph.invoke(Command(resume={"generate": True, "notes": ""}), config)
    gate = pending_interrupt(graph.get_state(config))
    assert gate and "review note" not in gate["question"] and gate["review_issues"] == []

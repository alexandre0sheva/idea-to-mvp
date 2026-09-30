import json

from plan_helpers import make_plan

from idea_to_mvp.blueprints import MAX_UPSTREAM_CHARS, bundle_file_plan, prompt_for, write_bundle
from idea_to_mvp.plan import Plan, render_plan_markdown, validate_plan
from idea_to_mvp.roles import PLAN_WRITER_SYSTEM

STRATEGY = {
    "mode": "agent_team",
    "reasoning": "Independent workstreams.",
    "workstreams": [
        {"name": "backend-api", "focus": "Build the API", "deliverables": "REST API with tests"},
        {"name": "web-ui", "focus": "Build the UI", "deliverables": "Frontend app"},
    ],
}


def _specs() -> dict:
    return {spec.relative_path: spec for spec in bundle_file_plan(STRATEGY)}


def test_bundle_file_plan_lists_all_documents() -> None:
    paths = [spec.relative_path for spec in bundle_file_plan(STRATEGY)]
    assert paths == [
        "README.md",
        "PRD.md",
        "ARCHITECTURE.md",
        "AGENTS.md",
        "contracts/AGENTS.md",
        "application/AGENTS.md",
        "quality/AGENTS.md",
        "plan.md",
        ".claude/agents/backend-api.md",
        ".claude/agents/web-ui.md",
    ]


def test_documents_are_assigned_to_three_dependency_waves() -> None:
    specs = _specs()
    assert {p: s.wave for p, s in specs.items() if s.wave == 1} == {"PRD.md": 1, "ARCHITECTURE.md": 1}
    wave_two = {"README.md", "AGENTS.md", "contracts/AGENTS.md", "application/AGENTS.md", "quality/AGENTS.md", "plan.md"}
    assert {p for p, s in specs.items() if s.wave == 2} == wave_two
    assert {p for p, s in specs.items() if s.wave == 3} == {".claude/agents/backend-api.md", ".claude/agents/web-ui.md"}


def test_plan_needs_the_prd_and_architecture_and_subagents_need_the_plan() -> None:
    specs = _specs()
    assert set(specs["plan.md"].needs) == {"PRD.md", "ARCHITECTURE.md"}
    for path in (".claude/agents/backend-api.md", ".claude/agents/web-ui.md"):
        assert "plan.md" in specs[path].needs


def test_the_plan_is_the_one_structured_document() -> None:
    specs = _specs()
    assert [p for p, s in specs.items() if s.structured] == ["plan.md"]
    assert specs["plan.md"].system_prompt == PLAN_WRITER_SYSTEM
    fallback_plan_text = specs["plan.md"].fallback
    assert "## Contract Registry" in fallback_plan_text and "### T01." in fallback_plan_text


def test_every_document_only_needs_documents_from_an_earlier_wave() -> None:
    specs = _specs()
    for spec in specs.values():
        for needed in spec.needs:
            assert specs[needed].wave < spec.wave, f"{spec.relative_path} needs {needed}"
        assert not spec.needs or spec.wave > 1


# ----------------------------------------------------------------- prompt_for


def test_a_document_without_upstream_needs_gets_the_context_and_its_instruction_only() -> None:
    prd = _specs()["PRD.md"]
    assert prompt_for(prd, "CONTEXT", {"plan.md": "ignored"}) == f"CONTEXT\n\n{prd.instruction}"


def test_upstream_documents_are_injected_under_headings_before_the_instruction() -> None:
    plan = _specs()["plan.md"]
    prompt = prompt_for(plan, "CONTEXT", {"PRD.md": "R1: log a climb", "ARCHITECTURE.md": "FastAPI monolith"})
    assert "## Upstream document: PRD.md\n\nR1: log a climb" in prompt
    assert "## Upstream document: ARCHITECTURE.md\n\nFastAPI monolith" in prompt
    assert prompt.startswith("CONTEXT") and prompt.endswith(plan.instruction)


def test_only_the_needed_and_available_documents_are_injected() -> None:
    plan = _specs()["plan.md"]
    prompt = prompt_for(plan, "CONTEXT", {"PRD.md": "R1", "README.md": "unrelated", "ARCHITECTURE.md": "  "})
    assert "Upstream document: PRD.md" in prompt
    assert "unrelated" not in prompt and "Upstream document: ARCHITECTURE.md" not in prompt


def test_long_upstream_documents_are_truncated_at_a_line_break_with_a_marker() -> None:
    plan = _specs()["plan.md"]
    long_prd = "\n".join(f"R{i}: requirement number {i} with some detail" for i in range(2000))
    prompt = prompt_for(plan, "CONTEXT", {"PRD.md": long_prd})
    assert "[truncated]" in prompt
    assert len(prompt) < MAX_UPSTREAM_CHARS + 1000
    body = prompt.split("## Upstream document: PRD.md\n\n", 1)[1].split("\n\n[truncated]", 1)[0]
    assert long_prd.startswith(body) and body.splitlines()[-1] in long_prd.splitlines()  # no half-cut line
    short = prompt_for(plan, "CONTEXT", {"PRD.md": "R1"})
    assert "[truncated]" not in short


def test_revision_prompts_carry_the_issues_and_the_previous_version_before_the_instruction() -> None:
    prd = _specs()["PRD.md"]
    prompt = prompt_for(prd, "CONTEXT", {}, issues="- [blocker] R4 has no owner", previous="# PRD\n- R4: ???")
    assert "## Review issues to fix in this revision\n\n- [blocker] R4 has no owner" in prompt
    assert "## Your previous version of PRD.md\n\n# PRD\n- R4: ???" in prompt
    assert prompt.startswith("CONTEXT") and prompt.endswith(prd.instruction)
    assert "Review issues" not in prompt_for(prd, "CONTEXT", {}) and "previous version" not in prompt_for(prd, "CONTEXT", {})


# --------------------------------------------------------------- write_bundle


def test_write_bundle_writes_all_files(tmp_path) -> None:
    docs = {spec.relative_path: f"GENERATED {spec.relative_path}" for spec in bundle_file_plan(STRATEGY)}
    docs["plan.json"] = make_plan().model_dump_json(indent=2)
    bundle_dir, created, summary = write_bundle(
        root=tmp_path, user_idea="A todo app for plumbers", docs=docs, strategy=STRATEGY
    )
    assert (bundle_dir / "PRD.md").read_text() == "GENERATED PRD.md\n"
    assert (bundle_dir / ".claude/agents/backend-api.md").exists()
    assert (bundle_dir / ".claude/agents/web-ui.md").exists()
    strategy_data = json.loads((bundle_dir / "STRATEGY.json").read_text())
    assert strategy_data["mode"] == "agent_team"
    expected = [s.relative_path for s in bundle_file_plan(STRATEGY)]
    expected.insert(expected.index("plan.md") + 1, "plan.json")
    assert created == [*expected, "STRATEGY.json"]
    assert str(bundle_dir) in summary and "a-todo-app-for-plumbers" in bundle_dir.name


def test_subagent_files_have_frontmatter(tmp_path) -> None:
    docs = {".claude/agents/backend-api.md": "You are a focused implementation agent."}
    bundle_dir, _, _ = write_bundle(root=tmp_path, user_idea="idea", docs=docs, strategy=STRATEGY)
    content = (bundle_dir / ".claude/agents/backend-api.md").read_text()
    assert content.startswith("---\n")
    assert "name: backend-api" in content and "description:" in content
    assert content.rstrip().endswith("You are a focused implementation agent.")


def test_empty_or_missing_documents_fall_back_and_strategy_json_is_always_written(tmp_path) -> None:
    bundle_dir, created, _ = write_bundle(
        root=tmp_path, user_idea="idea", docs={"PRD.md": "   ", "plan.md": ""}, strategy=STRATEGY
    )
    specs = _specs()
    for path in ("PRD.md", "plan.md", "AGENTS.md", ".claude/agents/web-ui.md"):
        assert specs[path].fallback.strip() in (bundle_dir / path).read_text(), path
    assert json.loads((bundle_dir / "STRATEGY.json").read_text())["workstreams"][1]["name"] == "web-ui"
    assert created[-1] == "STRATEGY.json"


def test_write_bundle_without_a_strategy_writes_the_eight_base_documents(tmp_path) -> None:
    bundle_dir, created, _ = write_bundle(root=tmp_path, user_idea="idea", docs={}, strategy=None)
    assert len(created) == 8 + 1 + 1 and not (bundle_dir / ".claude").exists()  # + plan.json, STRATEGY.json
    assert json.loads((bundle_dir / "STRATEGY.json").read_text()) == {}


# ------------------------------------------------- plan.json is the source of truth


def test_plan_md_is_always_rendered_from_plan_json(tmp_path) -> None:
    plan = make_plan()
    docs = {"plan.json": plan.model_dump_json(indent=2), "plan.md": "stale prose that must not win"}
    bundle_dir, created, _ = write_bundle(root=tmp_path, user_idea="idea", docs=docs, strategy=STRATEGY)
    assert (bundle_dir / "plan.md").read_text().strip() == render_plan_markdown(plan).strip()
    assert Plan.model_validate_json((bundle_dir / "plan.json").read_text()) == plan
    assert "plan.json" in created


def test_a_missing_or_broken_plan_json_is_replaced_by_a_valid_fallback_plan(tmp_path) -> None:
    prd = "- R1 (P0): log a climb\n- R2 (P0): history\n"
    for docs in ({"PRD.md": prd}, {"PRD.md": prd, "plan.json": "{not json"}):
        bundle_dir, _, _ = write_bundle(root=tmp_path, user_idea="idea", docs=docs, strategy=STRATEGY)
        plan = Plan.model_validate_json((bundle_dir / "plan.json").read_text())
        assert validate_plan(plan, prd_markdown=prd, workstreams=["backend-api", "web-ui"]) == []
        assert "### T01." in (bundle_dir / "plan.md").read_text()


# ------------------------------------------------------------------- REVIEW.md


def test_review_notes_are_written_listed_and_summarised(tmp_path) -> None:
    docs = {"REVIEW.md": "# Blueprint review\n\n- R3 has no owner\n"}
    bundle_dir, created, summary = write_bundle(
        root=tmp_path, user_idea="idea", docs=docs, strategy=STRATEGY, review_issues=["R3 has no owner"]
    )
    assert "R3 has no owner" in (bundle_dir / "REVIEW.md").read_text()
    assert "REVIEW.md" in created and "1 open review note" in summary and "R3 has no owner" in summary


def test_no_review_document_when_there_is_nothing_to_report(tmp_path) -> None:
    bundle_dir, created, summary = write_bundle(root=tmp_path, user_idea="idea", docs={}, strategy=STRATEGY)
    assert not (bundle_dir / "REVIEW.md").exists() and "REVIEW.md" not in created
    assert "review note" not in summary

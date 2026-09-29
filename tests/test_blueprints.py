import json

from idea_to_mvp.blueprints import bundle_file_plan, create_project_bundle

STRATEGY = {
    "mode": "agent_team",
    "reasoning": "Independent workstreams.",
    "workstreams": [
        {"name": "backend-api", "focus": "Build the API", "deliverables": "REST API with tests"},
        {"name": "web-ui", "focus": "Build the UI", "deliverables": "Frontend app"},
    ],
}


def _fake_generate(system_prompt: str, instruction: str) -> str:
    return f"GENERATED ({instruction[:24]}...)"


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


def test_create_project_bundle_writes_all_files(tmp_path) -> None:
    bundle_dir, created, summary = create_project_bundle(
        root=tmp_path,
        user_idea="A todo app for plumbers",
        context_block="Original idea: ...",
        strategy=STRATEGY,
        generate_doc=_fake_generate,
    )
    assert (bundle_dir / "PRD.md").exists()
    assert (bundle_dir / ".claude/agents/backend-api.md").exists()
    assert (bundle_dir / ".claude/agents/web-ui.md").exists()
    strategy_data = json.loads((bundle_dir / "STRATEGY.json").read_text())
    assert strategy_data["mode"] == "agent_team"
    assert "STRATEGY.json" in created
    assert "plan.md" in created
    assert str(bundle_dir) in summary


def test_subagent_files_have_frontmatter(tmp_path) -> None:
    def generate(system_prompt: str, instruction: str) -> str:
        if "subagent definition" in instruction:
            return "You are a focused implementation agent."
        return "doc"

    bundle_dir, _, _ = create_project_bundle(
        root=tmp_path,
        user_idea="idea",
        context_block="ctx",
        strategy=STRATEGY,
        generate_doc=generate,
    )
    content = (bundle_dir / ".claude/agents/backend-api.md").read_text()
    assert content.startswith("---\n")
    assert "name: backend-api" in content
    assert "description:" in content


def test_fallback_content_on_empty_generation(tmp_path) -> None:
    bundle_dir, _, _ = create_project_bundle(
        root=tmp_path,
        user_idea="idea",
        context_block="ctx",
        strategy=STRATEGY,
        generate_doc=lambda system_prompt, instruction: "",
    )
    assert (bundle_dir / "plan.md").read_text().strip()
    assert (bundle_dir / "AGENTS.md").read_text().strip()

from implementer import (
    _build_agent_definitions,
    _parse_verdict,
    _subagent_prompt,
    prepare_workspace,
)

STRATEGY = {
    "mode": "subagents",
    "reasoning": "Small MVP.",
    "workstreams": [
        {"name": "backend-api", "focus": "Build the API", "deliverables": "REST API"},
        {"name": "web-ui", "focus": "Build the UI", "deliverables": "Frontend"},
    ],
}


def test_prepare_workspace_copies_bundle(tmp_path) -> None:
    bundle = tmp_path / "blueprints" / "20260101-000000-000000-my-idea"
    (bundle / ".claude" / "agents").mkdir(parents=True)
    (bundle / "plan.md").write_text("# plan")
    (bundle / ".claude" / "agents" / "backend-api.md").write_text("---\nname: backend-api\n---\n\nbody")

    projects_root = tmp_path / "generated_projects"
    workspace = prepare_workspace(bundle, projects_root)

    assert workspace.parent == projects_root
    assert (workspace / "plan.md").read_text() == "# plan"
    assert (workspace / ".claude" / "agents" / "backend-api.md").exists()

    # A second call must not collide with the existing workspace.
    second = prepare_workspace(bundle, projects_root)
    assert second != workspace
    assert (second / "plan.md").exists()


def test_parse_verdict() -> None:
    assert _parse_verdict("All good.\nVERDICT: PASS") is True
    assert _parse_verdict("3 tests failed\nVERDICT: FAIL") is False
    assert _parse_verdict("verdict: pass") is True
    assert _parse_verdict("Ran tests, results unclear.") is None
    assert _parse_verdict("") is None
    # Last verdict wins if the agent revises itself.
    assert _parse_verdict("VERDICT: FAIL ... fixed ... VERDICT: PASS") is True


def test_build_agent_definitions_maps_workstreams(tmp_path) -> None:
    definitions = _build_agent_definitions(STRATEGY, workspace=None)
    assert set(definitions) == {"backend-api", "web-ui"}
    assert "backend-api" in definitions["backend-api"].description
    assert definitions["backend-api"].prompt.strip()
    assert definitions["backend-api"].model == "inherit"


def test_subagent_prompt_prefers_workspace_definition(tmp_path) -> None:
    workspace = tmp_path / "ws"
    (workspace / ".claude" / "agents").mkdir(parents=True)
    (workspace / ".claude" / "agents" / "backend-api.md").write_text(
        "---\nname: backend-api\ndescription: x\n---\n\nCustom body from blueprint."
    )
    assert _subagent_prompt(workspace, "backend-api", "fallback") == "Custom body from blueprint."
    assert _subagent_prompt(workspace, "missing", "fallback") == "fallback"
    assert _subagent_prompt(None, "backend-api", "fallback") == "fallback"

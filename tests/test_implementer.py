from idea_to_mvp.implementer import (
    _build_agent_definitions,
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
    (bundle / ".claude" / "agents" / "backend-api.md").write_text(
        "---\nname: backend-api\n---\n\nbody"
    )

    projects_root = tmp_path / "generated_projects"
    workspace = prepare_workspace(bundle, projects_root)

    assert workspace.parent == projects_root
    assert (workspace / "plan.md").read_text() == "# plan"
    assert (workspace / ".claude" / "agents" / "backend-api.md").exists()

    # A second call must not collide with the existing workspace.
    second = prepare_workspace(bundle, projects_root)
    assert second != workspace
    assert (second / "plan.md").exists()


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


# ------------------------------------------- lead and workstream sessions are recorded


def _fake_sessions(monkeypatch, *, error: bool = False, crash: bool = False):
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    from idea_to_mvp import implementer

    prompts: list[str] = []

    async def messages(prompt, **_kwargs):
        prompts.append(prompt)
        yield AssistantMessage(content=[TextBlock(text="Building.")], model="claude-x")
        if crash:
            raise RuntimeError("agent process died")
        yield ResultMessage(
            subtype="error_max_turns" if error else "success", duration_ms=1, duration_api_ms=1, is_error=error,
            num_turns=7, session_id="s-1", total_cost_usd=0.4, result="Stopped." if error else "Built it.",
        )  # fmt: skip

    monkeypatch.setattr(implementer, "iter_agent_messages", messages)
    return prompts


def _settings(**overrides):
    from idea_to_mvp.config import Settings

    return Settings(_env_file=None, implementer_sandbox="off", **overrides)


def test_the_lead_session_is_in_the_ledger_with_its_trajectory(tmp_path, monkeypatch) -> None:
    from idea_to_mvp.implementation.ledger import load_ledger
    from idea_to_mvp.implementer import run_implementation

    _fake_sessions(monkeypatch)
    assert run_implementation(tmp_path, STRATEGY, _settings()) == "Built it."
    (row,) = load_ledger(tmp_path)
    assert (
        row["kind"],
        row["label"],
        row["task_id"],
        row["cost_usd"],
        row["turns"],
        row["outcome"],
    ) == ("task", "lead", None, 0.4, 7, "success")
    assert "Building." in (tmp_path / row["trajectory"]).read_text()


def test_every_workstream_session_of_a_team_has_its_own_row(tmp_path, monkeypatch) -> None:
    from idea_to_mvp.implementation.ledger import load_ledger
    from idea_to_mvp.implementer import run_implementation

    _fake_sessions(monkeypatch)
    run_implementation(tmp_path, {**STRATEGY, "mode": "agent_team"}, _settings())
    assert [r["label"] for r in load_ledger(tmp_path)] == [
        "workstream-backend-api",
        "workstream-web-ui",
    ]


def test_a_session_that_ends_in_an_error_or_dies_is_recorded_as_such(tmp_path, monkeypatch) -> None:
    import pytest

    from idea_to_mvp.implementation.ledger import load_ledger
    from idea_to_mvp.implementer import run_implementation

    _fake_sessions(monkeypatch, error=True)
    run_implementation(tmp_path, STRATEGY, _settings())
    _fake_sessions(monkeypatch, crash=True)
    with pytest.raises(RuntimeError, match="agent process died"):
        run_implementation(tmp_path, STRATEGY, _settings())
    assert [(r["label"], r["outcome"]) for r in load_ledger(tmp_path)] == [
        ("lead", "failed"),
        ("lead-2", "crashed"),
    ]

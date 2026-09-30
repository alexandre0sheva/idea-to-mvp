"""The implement gate tells the truth about how the agents will be confined."""

from pathlib import Path
from typing import Any

import pytest

from idea_to_mvp.config import clear_settings_cache
from idea_to_mvp.implementation import options as opts
from idea_to_mvp.nodes import gates
from idea_to_mvp.state import make_initial_state
from idea_to_mvp.ui.render import warning_block
from idea_to_mvp.ui.view import transcript_from_state


def gate_payload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **env: str) -> dict[str, Any]:
    monkeypatch.chdir(tmp_path)  # Settings reads .env from the working directory: never the developer's own
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DEMO_MODE", "false")
    clear_settings_cache()
    captured: dict[str, Any] = {}

    def fake_interrupt(payload: dict[str, Any]) -> dict[str, Any]:
        captured.update(payload)
        return {"implement": False, "notes": ""}

    monkeypatch.setattr(gates, "interrupt", fake_interrupt)
    state = dict(make_initial_state("idea", 1))
    state["project_bundle_dir"] = "/tmp/bundle"
    gates.implement_gate_node(state)  # type: ignore[arg-type]
    clear_settings_cache()
    return captured


@pytest.fixture(autouse=True)
def sandbox_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(opts, "sandbox_supported", lambda *a, **k: True)


def test_a_sandboxed_run_states_the_sandbox_the_permission_mode_and_the_network_allowlist(monkeypatch, tmp_path) -> None:
    payload = gate_payload(monkeypatch, tmp_path, IMPLEMENTER_SANDBOX="on", IMPLEMENTER_ALLOWED_DOMAINS="pypi.org,crates.io")
    question = payload["question"]
    assert "Sandbox: on" in question and "acceptEdits" in question
    assert "pypi.org" in question and "crates.io" in question
    assert payload["sandbox"]["enabled"] is True and payload["permission_mode"] == "acceptEdits"
    assert payload["allowed_domains"] == ["pypi.org", "crates.io"]
    assert payload["warning"] == ""
    assert "file/shell access inside that workspace" not in question  # the old, inaccurate claim
    assert "$25.00" in question and "$5.00 per task" in question  # the whole-run cap and the task cap


def test_bypass_permissions_raises_a_warning(monkeypatch, tmp_path) -> None:
    payload = gate_payload(monkeypatch, tmp_path, IMPLEMENTER_SANDBOX="on", IMPLEMENTER_PERMISSION_MODE="bypassPermissions")
    assert "bypassPermissions" in payload["warning"] and "without asking" in payload["warning"]


def test_a_disabled_sandbox_raises_a_warning_and_the_question_says_so(monkeypatch, tmp_path) -> None:
    payload = gate_payload(monkeypatch, tmp_path, IMPLEMENTER_SANDBOX="off")
    assert "Sandbox: OFF" in payload["question"]
    assert "sandbox is off" in payload["warning"].lower() and "full user permissions" in payload["warning"]


def test_an_unsupported_platform_is_reported_honestly_under_auto(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(opts, "sandbox_supported", lambda *a, **k: False)
    payload = gate_payload(monkeypatch, tmp_path, IMPLEMENTER_SANDBOX="auto")
    assert payload["sandbox"]["enabled"] is False and "not supported" in payload["question"]
    assert payload["warning"]


def test_the_warning_is_rendered_as_a_red_card_after_the_gate_question() -> None:
    values = dict(make_initial_state("idea", 1))
    values["stage"] = "plan_bundle"
    entries = transcript_from_state(
        values, {"kind": "implement_gate", "question": "Start?", "warning": "Sandbox is OFF."}
    )
    assert [e.kind for e in entries[-2:]] == ["implement_gate", "warning"]
    assert entries[-1].content == "Sandbox is OFF."
    html = warning_block("Security warning", "Sandbox is OFF.")
    assert "warning-card" in html and "Sandbox is OFF." in html
    no_warning = transcript_from_state(values, {"kind": "implement_gate", "question": "Start?", "warning": ""})
    assert "warning" not in [e.kind for e in no_warning]



def test_the_gate_reports_the_plans_size_and_how_much_of_it_can_run_in_parallel(monkeypatch, tmp_path) -> None:
    from plan_helpers import make_plan, make_task

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    plan = make_plan([make_task("T01"), make_task("T02", depends_on=["T01"]), make_task("T03", depends_on=["T01"]), make_task("T04", depends_on=["T02", "T03"])])
    (bundle / "plan.json").write_text(plan.model_dump_json())
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.setenv("IMPLEMENTER_MAX_PARALLEL", "3")
    clear_settings_cache()
    captured: dict[str, Any] = {}
    monkeypatch.setattr(gates, "interrupt", lambda payload: captured.update(payload) or {"implement": False, "notes": ""})
    state = dict(make_initial_state("idea", 1))
    state["project_bundle_dir"] = str(bundle)
    state["execution_strategy"] = {"mode": "agent_team", "reasoning": "", "workstreams": []}
    gates.implement_gate_node(state)  # type: ignore[arg-type]
    assert (captured["task_count"], captured["dag_width"], captured["max_parallel"]) == (4, 2, 3)
    assert "4 tasks" in captured["question"] and "2 of them can run in parallel" in captured["question"]
    monkeypatch.setenv("IMPLEMENTER_MAX_PARALLEL", "1")
    clear_settings_cache()
    captured.clear()
    gates.implement_gate_node(state)  # type: ignore[arg-type]
    assert "one at a time" in captured["question"] and captured["max_parallel"] == 1
    (bundle / "plan.json").unlink()
    captured.clear()
    gates.implement_gate_node(state)  # type: ignore[arg-type]
    assert captured["task_count"] == 0
    assert "can run in parallel" not in captured["question"] and "one at a time" not in captured["question"]
    clear_settings_cache()

"""Full pipeline, offline: real graph on a real SQLite checkpointer + real SubmitService + demo models."""

from pathlib import Path

from service_helpers import POS_STATUS, chat_text, mode_of, submit, update_value

from idea_to_mvp.ui.view import (
    ARCH_CHOICE_A,
    IMPL_CHOICE_START,
    MODE_ANSWERS,
    MODE_ARCH_CHOICE,
    MODE_DONE,
    MODE_IDEA,
    MODE_IMPL_GATE,
    MODE_PLAN_GATE,
    PLAN_CHOICE_GENERATE,
)


async def test_demo_mode_runs_whole_pipeline_offline(demo_service, demo_env: Path) -> None:
    service, graphs = demo_service
    thread = "e2e-demo"
    assert await mode_of(service, thread) == MODE_IDEA

    outputs = await submit(service, "A habit tracker for climbing gyms", "", thread)
    assert await mode_of(service, thread) == MODE_ANSWERS
    text = chat_text(outputs[-1])
    assert "habit tracker" in text.lower()
    assert "Suggested answer" in text  # typed questions reach the UI
    assert len(outputs) > 3  # streamed: several renders, not one

    outputs = await submit(service, "1. Solo climbers.\n2. Logging only.", "", thread)
    assert await mode_of(service, thread) == MODE_ARCH_CHOICE

    await submit(service, "", ARCH_CHOICE_A, thread)
    assert await mode_of(service, thread) == MODE_PLAN_GATE

    await submit(service, "", PLAN_CHOICE_GENERATE, thread)
    assert await mode_of(service, thread) == MODE_IMPL_GATE

    outputs = await submit(service, "", IMPL_CHOICE_START, thread)
    assert await mode_of(service, thread) == MODE_DONE

    graph = await graphs.get()
    values = (await graph.aget_state({"configurable": {"thread_id": thread}})).values
    assert values["stage"] == "done"
    assert values["verification"]["passed"] is True

    workspace = Path(values["workspace_dir"])
    assert workspace.is_dir() and workspace.is_relative_to(demo_env / "projects")
    assert (workspace / "plan.md").exists() and (workspace / "PRD.md").exists()
    assert str(workspace) in values["delivery_report"]

    bundle = Path(values["project_bundle_dir"])
    assert bundle.is_relative_to(demo_env / "blueprints")
    assert (bundle / "STRATEGY.json").exists()
    assert any(bundle.glob(".claude/agents/*.md"))

    final_chat = chat_text(outputs[-1])
    assert "Delivery report" in final_chat
    # the finished run is fully reconstructible from the checkpoint alone
    assert "Architecture choice: Option A" in final_chat and "Start implementation" in final_chat

    # every text-generating node reported usage into state, and the UI shows the running total
    roles = {record["role"] for record in values["usage"]}
    assert {"pm", "tech_lead", "skeptic", "summarizer", "architect"} <= roles
    assert all(r["input_tokens"] > 0 and r["output_tokens"] > 0 for r in values["usage"])
    assert " in / " in update_value(outputs[-1][POS_STATUS], "value")

    sessions = service._context.sessions.list()
    assert [s.thread_id for s in sessions] == [thread]
    assert sessions[0].stage == "done" and sessions[0].title.startswith("A habit tracker")

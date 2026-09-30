"""Full pipeline, offline: real graph on a real SQLite checkpointer + real SubmitService + demo models."""

from pathlib import Path

from service_helpers import (
    POS_STATUS,
    POS_TRACKER,
    chat_text,
    gate_field,
    mode_of,
    panel_visible,
    submit,
    update_value,
)

from idea_to_mvp.plan import Plan, validate_plan
from idea_to_mvp.ui.view import (
    ARCH_CHOICE_A,
    IMPL_CHOICE_START,
    ITERATE_CHOICE_CHANGES,
    ITERATE_CHOICE_DONE,
    MODE_ANSWERS,
    MODE_ARCH_CHOICE,
    MODE_DONE,
    MODE_IDEA,
    MODE_IMPL_GATE,
    MODE_ITERATE_GATE,
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
    texts = [chat_text(output) for output in outputs]
    statuses = [str(update_value(output[POS_STATUS], "value")) for output in outputs]
    assert any("Implementing T01" in s and "of $25.00" in s for s in statuses)  # live per-task progress + running $
    assert any("Core journey" in t and "thinking" in t for t in texts)  # the progress is also shown in the chat
    assert await mode_of(service, thread) == MODE_ITERATE_GATE  # delivered: the user may ask for changes

    graph = await graphs.get()
    values = (await graph.aget_state({"configurable": {"thread_id": thread}})).values
    assert values["stage"] == "done"
    assert values["verification"]["passed"] is True

    workspace = Path(values["workspace_dir"])
    assert workspace.is_dir() and workspace.is_relative_to(demo_env / "projects")
    assert (workspace / "plan.md").exists() and (workspace / "PRD.md").exists()
    assert str(workspace) in values["delivery_report"]
    assert list(values["task_results"]) == ["T01", "T02", "T03"] and (workspace / ".git").is_dir()
    assert (workspace / ".idea-to-mvp" / "progress.json").exists()

    bundle = Path(values["project_bundle_dir"])
    assert bundle.is_relative_to(demo_env / "blueprints")
    assert (bundle / "STRATEGY.json").exists()
    assert any(bundle.glob(".claude/agents/*.md"))
    plan = Plan.model_validate_json((bundle / "plan.json").read_text())
    assert validate_plan(plan, prd_markdown=(bundle / "PRD.md").read_text(), workstreams=["core-app", "quality"]) == []
    assert "### T01." in (bundle / "plan.md").read_text() and not (bundle / "REVIEW.md").exists()

    final_chat = chat_text(outputs[-1])
    assert "Delivery report" in final_chat
    # the finished run is fully reconstructible from the checkpoint alone
    assert "Architecture choice: Option A" in final_chat and "Start implementation" in final_chat

    # every text-generating node reported usage into state, and the UI shows the running total
    roles = {record["role"] for record in values["usage"]}
    assert {"pm", "tech_lead", "skeptic", "summarizer", "architect"} <= roles
    assert all(r["input_tokens"] > 0 and r["output_tokens"] > 0 for r in values["usage"])
    assert "tokens</span>" in update_value(outputs[-1][POS_TRACKER], "value")  # ... and the header badge shows the total

    sessions = service._context.sessions.list()
    assert [s.thread_id for s in sessions] == [thread]
    assert sessions[0].stage == "done" and sessions[0].title.startswith("A habit tracker")


async def test_the_delivered_project_can_be_iterated_on_from_the_ui(demo_service, demo_env: Path) -> None:
    service, graphs = demo_service
    thread = "e2e-iterate"
    await submit(service, "A habit tracker for climbing gyms", "", thread)
    await submit(service, "1. Solo climbers.", "", thread)
    await submit(service, "", ARCH_CHOICE_A, thread)
    await submit(service, "", PLAN_CHOICE_GENERATE, thread)
    outputs = await submit(service, "", IMPL_CHOICE_START, thread)
    assert await mode_of(service, thread) == MODE_ITERATE_GATE
    assert panel_visible(outputs[-1], "iterate_gate") is True and panel_visible(outputs[-1], "implement_gate") is False
    assert "v0.2" in update_value(gate_field(outputs[-1], "iterate_gate", "feedback"), "label")
    assert "Want changes?" in chat_text(outputs[-1]) and "v0.1" in chat_text(outputs[-1])

    # asking for changes without saying which changes nothing and says what is missing
    outputs = await submit(service, "", ITERATE_CHOICE_CHANGES, thread)
    assert await mode_of(service, thread) == MODE_ITERATE_GATE
    assert "Describe" in str(update_value(outputs[-1][POS_STATUS], "value"))

    outputs = await submit(service, "Add CSV export of the climb history.", ITERATE_CHOICE_CHANGES, thread)
    statuses = [str(update_value(output[POS_STATUS], "value")) for output in outputs]
    assert any("Planning your changes" in s for s in statuses) and any("I2-01" in s for s in statuses)
    assert await mode_of(service, thread) == MODE_ITERATE_GATE
    chat = chat_text(outputs[-1])
    assert "Change request (v0.2): Add CSV export of the climb history." in chat and "v0.2" in chat

    graph = await graphs.get()
    values = (await graph.aget_state({"configurable": {"thread_id": thread}})).values
    assert values["iteration"] == 2 and values["verification"]["passed"] is True
    assert list(values["task_results"]) == ["T01", "T02", "T03", "I2-01"]
    assert Path(values["delivery_zip"]).name.endswith("-v0.2.zip") and Path(values["delivery_zip"]).exists()

    await submit(service, "", ITERATE_CHOICE_DONE, thread)
    assert await mode_of(service, thread) == MODE_DONE

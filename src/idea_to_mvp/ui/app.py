from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any, cast

import gradio as gr

from idea_to_mvp.config import Settings, get_settings
from idea_to_mvp.graph import GraphProvider
from idea_to_mvp.observability import apply_tracing_env
from idea_to_mvp.sessions import SessionRegistry
from idea_to_mvp.ui.components import EXAMPLE_IDEAS, model_profile_markdown, stage_header
from idea_to_mvp.ui.console import render_console, render_cost_meter, render_task_board
from idea_to_mvp.ui.gates import GATES, GateRuntime, gate_layout, pack_inputs
from idea_to_mvp.ui.service import AppContext, SubmitService
from idea_to_mvp.ui.theme import CSS, build_theme

PANEL_MODES = ["moderated", "round_robin"]
PLATFORMS = ["web", "mobile", "cli", "api", "any"]


def make_ui(
    *,
    settings: Settings | None = None,
    graphs: GraphProvider | None = None,
    registry: SessionRegistry | None = None,
) -> gr.Blocks:
    resolved_settings = settings or get_settings()
    context = AppContext(
        settings=resolved_settings,
        graphs=graphs or GraphProvider(resolved_settings),
        registry=registry,
    )
    service = SubmitService(context)

    with gr.Blocks(title="Idea to MVP Orchestrator") as demo:
        with gr.Row(equal_height=False):
            with gr.Column(scale=3):
                gr.Markdown(
                    "## Idea-to-MVP Orchestrator\n"
                    "From a rough idea to a built, tested first version — panel debate, architecture, "
                    "blueprints, and autonomous implementation agents, with you approving every gate."
                )
            with gr.Column(scale=2):
                with gr.Accordion("Settings", open=False):
                    rounds_sl = gr.Slider(
                        minimum=1,
                        maximum=6,
                        value=resolved_settings.default_rounds,
                        step=1,
                        label="Rounds per speaker",
                    )
                    panel_mode_dd = gr.Dropdown(
                        choices=PANEL_MODES,
                        value=resolved_settings.panel_mode,
                        label="Panel mode",
                        info="moderated: parallel openings, a moderator picks speakers. round_robin: fixed rotation.",
                    )
                    autopilot_cb = gr.Checkbox(
                        value=False,
                        label="Autopilot",
                        info="Answer the questions with their suggestions, take the recommended architecture, and "
                        "generate the pack. Implementation always asks first: it spends real money.",
                    )
                    gr.Markdown(model_profile_markdown(resolved_settings))
                with gr.Accordion("Project preferences", open=False):
                    gr.Markdown(
                        "What you already know, so the panel does not debate it. Must use / must avoid are "
                        "hard constraints for every option and document. Applies to the next idea you run."
                    )
                    platform_dd = gr.Dropdown(choices=PLATFORMS, value="any", label="Platform")
                    stack_tb = gr.Textbox(label="Stack hints", placeholder="TypeScript + Postgres")
                    deploy_tb = gr.Textbox(label="Deploy target", placeholder="Fly.io")
                    must_use_tb = gr.Textbox(label="Must use (hard constraint)", placeholder="Stripe for payments")
                    must_avoid_tb = gr.Textbox(label="Must avoid (hard constraint)", placeholder="MongoDB")
        if resolved_settings.demo_mode:
            gr.Markdown(
                "> **Demo mode** — model outputs are canned and the implementation stage builds a tiny "
                "stand-in project. No API calls, no cost. Unset `DEMO_MODE` to use real models."
            )
        tracker_html = gr.HTML(stage_header("discussion", {}, {}, {}))
        status_md = gr.Markdown("")
        thread_id_state = gr.State(str(uuid.uuid4()))
        panels: dict[str, gr.Column] = {}
        gate_widgets: dict[str, dict[str, Any]] = {}
        # The service selects the implementation tab while a run builds and verifies, and the conversation
        # otherwise (only when that changes, so the user can look at the other tab meanwhile).
        with gr.Tabs(selected="conversation") as tabs:
            with gr.Tab("Conversation", id="conversation"):
                chatbot = gr.Chatbot(label="Discussion chat", height="70vh")
                input_tb = gr.Textbox(label="Describe your idea", lines=4, placeholder="Describe your product idea...")
                with gr.Column(visible=True) as examples_box:
                    gr.Examples(examples=[[idea] for idea in EXAMPLE_IDEAS], inputs=[input_tb], label="Try an example idea")
                run_btn = gr.Button("Run discussion", variant="primary")
                # One form per gate, shown only while the graph waits at that gate (see ui/gates.py).
                for kind, spec in GATES.items():
                    with gr.Column(visible=False, elem_classes=["gate-form"]) as panels[kind]:
                        gate_widgets[kind] = spec.build()
            with gr.Tab("Implementation", id="implementation"):
                meter_html = gr.HTML(render_cost_meter(0.0, resolved_settings.implementer_max_total_usd))
                board_html = gr.HTML(render_task_board({}, {}, set()))
                console_html = gr.HTML(render_console([]))
                with gr.Accordion("Project files and downloads", open=False):
                    path_tb = gr.Textbox(label="Workspace folder", interactive=False, buttons=["copy"])
                    with gr.Row():
                        delivery_btn = gr.DownloadButton("Download the project (zip)", visible=False)
                        blueprint_btn = gr.DownloadButton("Download the blueprint (zip)", visible=False)
                    workspace_state = gr.State("")

                    # A FileExplorer's root cannot change once it is built, so it is rebuilt whenever the workspace
                    # does. It only lists files (read-only); their text comes through `service.view_file`, which
                    # stays inside the workspace.
                    @gr.render(inputs=[workspace_state])
                    def files(workspace: str) -> None:
                        if not workspace or not Path(workspace).is_dir():
                            gr.Markdown("The project's files appear here once it is built.")
                            return
                        explorer = gr.FileExplorer(
                            root_dir=workspace, glob="**/*", ignore_glob="**/.git/**", file_count="single",
                            label="Files (read-only)", height=320,
                        )
                        explorer.change(service.view_file, inputs=[thread_id_state, explorer], outputs=[file_view])
                
                    file_view = gr.Code(label="Selected file", interactive=False, lines=14)
        field_components = [gate_widgets[kind][name] for kind, name in gate_layout()]
        with gr.Row():
            stop_btn = gr.Button("Stop", variant="stop")
            save_btn = gr.Button("Save session")
            clear_btn = gr.Button("Clear")
        export_file = gr.File(label="Saved session (Markdown and JSON)", file_count="multiple", visible=False)
        with gr.Accordion("Saved sessions", open=False):
            sessions_dd = gr.Dropdown(label="Sessions (resume after a restart)", choices=[], value=None)
            with gr.Row():
                resume_btn = gr.Button("Resume selected")
                delete_btn = gr.Button("Delete selected", variant="stop")

        # The browser session only remembers which graph thread it is looking at (`thread_id_state`, created
        # above); everything else (chat, mode, status, the gate form) is derived from that thread's checkpoint.
        view_outputs = [
            status_md,
            chatbot,
            input_tb,
            rounds_sl,
            run_btn,
            thread_id_state,
            tracker_html,
            sessions_dd,
            examples_box,
            tabs,
            board_html,
            console_html,
            meter_html,
            path_tb,
            delivery_btn,
            blueprint_btn,
            workspace_state,
            *panels.values(),
            *field_components,
        ]
        shared_inputs = [input_tb, rounds_sl, thread_id_state, panel_mode_dd, autopilot_cb]
        preference_inputs = [platform_dd, stack_tb, deploy_tb, must_use_tb, must_avoid_tb]

        async def submit_idea(*values: Any) -> AsyncGenerator[tuple[Any, ...], None]:
            text, rounds, thread, panel_mode, autopilot, *preference_values = values
            preferences = dict(
                zip(("platform", "stack_hints", "deploy_target", "must_use", "must_avoid"), preference_values, strict=True)
            )
            async for update in service.handle_submit(text, rounds, thread, panel_mode, autopilot, preferences=preferences):
                yield update

        run_events = [run_btn.click(submit_idea, inputs=[*shared_inputs, *preference_inputs], outputs=view_outputs)]

        def submit_for(kind: str, action: str) -> Callable[..., AsyncGenerator[tuple[Any, ...], None]]:
            async def submit(*values: Any) -> AsyncGenerator[tuple[Any, ...], None]:
                text, rounds, thread, panel_mode, autopilot, *gate_values = values
                inputs = pack_inputs(kind, action, gate_values)
                async for update in service.handle_submit(text, rounds, thread, panel_mode, autopilot, gate_inputs=inputs):
                    yield update

            return submit

        for kind, spec in GATES.items():
            for gate_action in spec.actions:
                button = gate_widgets[kind][f"action:{gate_action.name}"]
                run_events.append(
                    button.click(submit_for(kind, gate_action.name), inputs=[*shared_inputs, *field_components], outputs=view_outputs)
                )
            spec.wire(gate_widgets[kind], GateRuntime(thread=thread_id_state, payload=service.gate_payload))
        # Cancelling a run event cancels the graph run cooperatively (the checkpoint keeps its state);
        # the handler then shows where the run stands and offers to continue.
        stop_btn.click(service.stop_run, inputs=[thread_id_state], outputs=view_outputs, cancels=cast(Any, run_events))
        save_btn.click(
            service.save_conversation,
            inputs=[input_tb, thread_id_state],
            outputs=[status_md, export_file],
        )
        clear_btn.click(service.clear_session, outputs=view_outputs)
        clear_btn.click(lambda: gr.update(value=None, visible=False), outputs=[export_file])
        resume_btn.click(
            service.load_session, inputs=[sessions_dd, thread_id_state], outputs=view_outputs
        )
        delete_btn.click(
            service.delete_session, inputs=[sessions_dd, thread_id_state], outputs=view_outputs
        )
        demo.load(service.initial_view, inputs=[thread_id_state], outputs=view_outputs)
    return demo


def main() -> None:
    apply_tracing_env()
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    # Gradio 6 takes css/theme in launch(), not in Blocks().
    settings = get_settings()
    settings.deliveries_dir.mkdir(parents=True, exist_ok=True)
    # Downloads are served from the deliveries folder only (zips of the project and of the blueprint).
    make_ui().launch(inbrowser=True, css=CSS, theme=build_theme(), allowed_paths=[str(settings.deliveries_dir)])


if __name__ == "__main__":
    main()

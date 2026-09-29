from __future__ import annotations

import logging
import uuid

import gradio as gr

from idea_to_mvp.config import Settings, get_settings
from idea_to_mvp.graph import GraphProvider
from idea_to_mvp.observability import apply_tracing_env
from idea_to_mvp.sessions import SessionRegistry
from idea_to_mvp.ui.render import DEFAULT_IDEA, PANEL_CSS, stage_tracker
from idea_to_mvp.ui.service import ARCH_CHOICE_A, ARCH_CHOICE_B, AppContext, SubmitService


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
        gr.Markdown(
            "## Idea-to-MVP Orchestrator\n"
            "From a rough idea to a built, tested first version — panel debate, architecture, "
            "blueprints, and autonomous implementation agents, with you approving every gate."
        )
        if resolved_settings.demo_mode:
            gr.Markdown(
                "> **Demo mode** — model outputs are canned and the implementation stage builds a tiny "
                "stand-in project. No API calls, no cost. Unset `DEMO_MODE` to use real models."
            )
        tracker_html = gr.HTML(stage_tracker("discussion"))
        status_md = gr.Markdown("")
        chatbot = gr.Chatbot(label="Discussion chat", height=700)
        input_tb = gr.Textbox(label="Describe your idea", value=DEFAULT_IDEA, lines=5)
        rounds_sl = gr.Slider(
            minimum=1,
            maximum=6,
            value=resolved_settings.default_rounds,
            step=1,
            label="Rounds per speaker",
        )
        decision_radio = gr.Radio(
            choices=[ARCH_CHOICE_A, ARCH_CHOICE_B],
            value=ARCH_CHOICE_A,
            label="Decision",
            visible=False,
        )
        with gr.Row():
            run_btn = gr.Button("Run discussion", variant="primary")
            save_btn = gr.Button("Save to Markdown")
            clear_btn = gr.Button("Clear")
        export_file = gr.File(label="Saved Markdown file", visible=False)
        with gr.Accordion("Saved sessions", open=False):
            sessions_dd = gr.Dropdown(label="Sessions (resume after a restart)", choices=[], value=None)
            with gr.Row():
                resume_btn = gr.Button("Resume selected")
                delete_btn = gr.Button("Delete selected", variant="stop")

        # The browser session only remembers which graph thread it is looking at; everything else
        # (chat, mode, status) is derived from that thread's checkpoint.
        thread_id_state = gr.State(str(uuid.uuid4()))

        view_outputs = [
            status_md,
            chatbot,
            input_tb,
            rounds_sl,
            decision_radio,
            run_btn,
            thread_id_state,
            tracker_html,
            sessions_dd,
        ]
        run_btn.click(
            service.handle_submit,
            inputs=[input_tb, rounds_sl, decision_radio, thread_id_state],
            outputs=view_outputs,
        )
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
    make_ui().launch(inbrowser=True, css=PANEL_CSS)


if __name__ == "__main__":
    main()

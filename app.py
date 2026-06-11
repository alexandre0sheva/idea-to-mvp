from __future__ import annotations

import inspect
import logging
import uuid

import gradio as gr

try:
    from .config import Settings, get_settings
    from .graph import build_graph
    from .render import DEFAULT_IDEA, PANEL_CSS, stage_tracker
    from .submit_service import ARCH_CHOICE_A, ARCH_CHOICE_B, AppContext, SubmitService
except ImportError:
    from config import Settings, get_settings
    from graph import build_graph
    from render import DEFAULT_IDEA, PANEL_CSS, stage_tracker
    from submit_service import ARCH_CHOICE_A, ARCH_CHOICE_B, AppContext, SubmitService


def _supports_param(callable_obj: object, param_name: str) -> bool:
    try:
        return param_name in inspect.signature(callable_obj).parameters
    except (TypeError, ValueError):
        return False


def make_ui(*, settings: Settings | None = None) -> gr.Blocks:
    resolved_settings = settings or get_settings()
    context = AppContext(
        settings=resolved_settings,
        graph=build_graph(resolved_settings.enable_checkpointer),
    )
    service = SubmitService(context)

    blocks_kwargs = {"title": "Idea to MVP Orchestrator"}
    if _supports_param(gr.Blocks.__init__, "css"):
        blocks_kwargs["css"] = PANEL_CSS

    with gr.Blocks(**blocks_kwargs) as demo:
        gr.Markdown(
            "## Idea-to-MVP Orchestrator\n"
            "From a rough idea to a built, tested first version — panel debate, architecture, "
            "blueprints, and autonomous implementation agents, with you approving every gate."
        )
        tracker_html = gr.HTML(stage_tracker("discussion"))
        status_md = gr.Markdown("")
        chatbot_kwargs = {"label": "Discussion chat", "height": 700}
        if _supports_param(gr.Chatbot.__init__, "type"):
            chatbot_kwargs["type"] = "messages"
        chatbot = gr.Chatbot(**chatbot_kwargs)
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

        thread_id_state = gr.State(str(uuid.uuid4()))
        mode_state = gr.State("idea")
        turns_state = gr.State([])
        transcript_state = gr.State([])
        chat_state = gr.State([])

        submit_outputs = [
            status_md,
            chatbot,
            input_tb,
            rounds_sl,
            decision_radio,
            run_btn,
            thread_id_state,
            mode_state,
            turns_state,
            transcript_state,
            chat_state,
            tracker_html,
        ]
        run_btn.click(
            service.handle_submit,
            inputs=[input_tb, rounds_sl, decision_radio, thread_id_state, mode_state, turns_state, transcript_state, chat_state],
            outputs=submit_outputs,
        )
        save_btn.click(
            service.save_conversation,
            inputs=[input_tb, thread_id_state, mode_state, transcript_state],
            outputs=[status_md, export_file],
        )
        clear_btn.click(service.clear_session, outputs=submit_outputs)
        clear_btn.click(
            lambda: gr.update(value=None, visible=False),
            outputs=[export_file],
        )
    return demo


def main() -> None:
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    launch_kwargs = {"inbrowser": True}
    if _supports_param(gr.Blocks.launch, "css"):
        launch_kwargs["css"] = PANEL_CSS
    make_ui().launch(**launch_kwargs)


if __name__ == "__main__":
    main()

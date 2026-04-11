from __future__ import annotations

import inspect
import logging
import uuid

import gradio as gr

try:
    from .config import Settings, get_settings
    from .graph import build_graph
    from .render import DEFAULT_IDEA, PANEL_CSS
    from .submit_service import AppContext, SubmitService
except ImportError:
    from config import Settings, get_settings
    from graph import build_graph
    from render import DEFAULT_IDEA, PANEL_CSS
    from submit_service import AppContext, SubmitService


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
        gr.Markdown("## LangGraph Idea-to-MVP Orchestrator")
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
        with gr.Row():
            run_btn = gr.Button("Run discussion", variant="primary")
            clear_btn = gr.Button("Clear")

        thread_id_state = gr.State(str(uuid.uuid4()))
        mode_state = gr.State("idea")
        questions_state = gr.State([])
        summary_state = gr.State("")
        turns_state = gr.State([])
        chat_state = gr.State([])

        run_btn.click(
            service.handle_submit,
            inputs=[input_tb, rounds_sl, thread_id_state, mode_state, questions_state, summary_state, turns_state, chat_state],
            outputs=[status_md, chatbot, input_tb, rounds_sl, run_btn, thread_id_state, mode_state, questions_state, summary_state, turns_state, chat_state],
        )
        clear_btn.click(
            service.clear_session,
            outputs=[status_md, chatbot, input_tb, rounds_sl, run_btn, thread_id_state, mode_state, questions_state, summary_state, turns_state, chat_state],
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

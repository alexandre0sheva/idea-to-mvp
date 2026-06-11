# Idea-to-MVP Orchestrator

`idea-to-mvp` is a Gradio + LangGraph app that turns a rough product idea into a structured build-ready output. It runs a multi-agent panel, generates a summary and MVP questions, produces architecture options, and can create an agent-ready project pack with scoped `AGENTS.md` files plus a task-only `plan.md`.

## What It Does

- Runs a multi-round discussion across `PM`, `Tech Lead`, and `Skeptic`.
- Produces a concise summary and five MVP decision questions.
- Generates two architecture options after the user answers follow-up questions.
- Offers a planner step that can create a project blueprint folder for downstream implementation agents.
- Saves the full visible session to Markdown at any time.
- Supports `openai`, `anthropic`, and `google` model providers.

## Workflow

1. Enter an idea and run the panel discussion (PM, Tech Lead, Skeptic debate over the rounds you choose).
2. The pipeline pauses at the first gate: review the summary and answer the five MVP decision questions.
3. The architect produces two architecture options anchored to your answers.
4. The pipeline pauses at the second gate: choose whether to generate the execution pack (`AGENTS.md` files and `plan.md`), optionally adding planning notes.
5. Save the session to `exports/` as Markdown at any time.

The whole session runs as a single LangGraph thread; the gates are real `interrupt()` pauses that resume exactly where the graph stopped.

## Quick Start

### Requirements

- Python 3.11+

### Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Configure

```bash
cp .env.example .env
```

Fill in the provider API keys you want to use and adjust model selections if needed.

### Run

```bash
python app.py
```

## Outputs

- `exports/`: saved conversation Markdown files.
- `project_blueprints/`: planner-generated project folders with `AGENTS.md` files and `plan.md`.

## Configuration

Settings are loaded from `config.py` and environment variables.

- `*_PROVIDER`: `openai`, `anthropic`, or `google`
- `*_MODEL`: model ID for each role
- `DISCUSSION_MAX_TOKENS`, `SKEPTIC_MAX_TOKENS`, `SUMMARY_MAX_TOKENS`: runtime output budgets
- `ENABLE_CHECKPOINTER`: enable or disable LangGraph in-memory checkpointing

## Development

```bash
ruff check .
pytest
```

## Project Layout

- `app.py`: Gradio UI entrypoint
- `submit_service.py`: stateful submit flow and streaming updates
- `agents.py`: model runtimes, LLM invocation, and graph nodes
- `roles.py`: declarative role registry (providers, models, token budgets, system prompts)
- `graph.py`: LangGraph construction
- `render.py`: UI rendering helpers
- `state.py`: shared graph state contract
- `exporter.py`: Markdown export builder

## Notes

- Multiple `AGENTS.md` files are supported and useful when each one has a clear scope.
- The planner output is meant for future agent-driven implementation, not just human reading.
- Never commit `.env` files or API keys.

## Security

See `SECURITY.md` for vulnerability reporting guidance.

## License

This project is licensed under the MIT License. See `LICENSE`.

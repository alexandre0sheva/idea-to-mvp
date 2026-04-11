# Idea-to-MVP Orchestrator

Run a multi-agent product panel (PM, Tech Lead, Skeptic) to pressure-test a startup idea, produce a practical summary, and generate architecture options from your answers.

## Features

- Multi-round panel discussion with role-specific prompts.
- Automatic summary and MVP decision questions.
- Follow-up architect stage with two architecture options.
- Multi-provider model support (`openai`, `anthropic`, `google`).
- Gradio UI with streaming conversation updates.

## Quick Start

### 1) Requirements

- Python 3.11+

### 2) Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3) Configure environment

```bash
cp .env.example .env
```

Fill in at least one provider API key in `.env` and select models/providers as needed.

### 4) Run

```bash
python app.py
```

## Configuration

Settings are defined in `config.py` and loaded from environment variables.

- `*_PROVIDER` values: `openai`, `anthropic`, `google`
- `*_MODEL` values: provider model IDs
- `ENABLE_CHECKPOINTER`: `true`/`false` for LangGraph memory checkpointing

## Development

Run lint and tests:

```bash
ruff check .
pytest
```

## Architecture

- `app.py`: Gradio UI wiring (thin entrypoint).
- `submit_service.py`: submit flow state machine and streaming orchestration.
- `render.py`: presentation helpers for speaker cards and markdown rendering.
- `agents.py`: model runtimes, prompt logic, graph nodes, and routing.
- `graph.py`: LangGraph construction and compilation.
- `state.py`: typed graph state contract.

## Optional Provider Footprint

By default, dependencies include all supported providers. If you only use one provider, you can prune unused provider SDKs in your own deployment fork.

## Security

Never commit `.env` files or API keys. See `SECURITY.md` for reporting details.

## License

This project is licensed under the MIT License. See `LICENSE`.

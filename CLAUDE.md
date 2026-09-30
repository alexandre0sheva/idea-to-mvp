# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Setup (uv manages the venv and Python; dependencies live only in pyproject.toml / uv.lock)
uv sync
cp .env.example .env  # then fill in API keys

# Run
uv run idea-to-mvp   # or: uv run python -m idea_to_mvp
uv run idea-to-mvp doctor [--offline]   # check keys/models/agent CLI

# Lint / types
uv run ruff check .
uv run mypy

# Test
uv run pytest                                         # all tests
uv run pytest tests/test_graph_lifecycle.py -v         # single file
uv run pytest tests/test_graph_lifecycle.py::test_name # single test
uv run pytest --cov=idea_to_mvp                        # what CI runs: fails below 80% coverage
```

## Architecture

Gradio + LangGraph app: idea → panel discussion → summary → gates → architecture → strategy → blueprint pack → implementation (Claude Agent SDK) → verification. The pipeline, module map, state contract, and conventions live in [docs/architecture.md](docs/architecture.md) — read it before changing graph topology, state, or prompts, and update it (only it) when they change. Every setting is documented in `.env.example`.

## Conventions

- Source is the `idea_to_mvp` package in `src/`; imports are absolute (no dual `try/except ImportError` imports).
- The UI derives everything from checkpointed state (`ui/view.py`); do not add UI-side copies of pipeline state. Async service tests use the `demo_service` fixture and `tests/service_helpers.py`.
- Tests call `clear_settings_cache()` in teardown and set `OUTPUT_DIR` to `tmp_path`; graph lifecycle tests fake LLM/SDK calls (`llm.get_runtime`, `llm.invoke_text`, `llm.invoke_structured`, `implementer.*`) as described in the architecture doc.
- New LLM calls / agent sessions need a fixture in `src/idea_to_mvp/demo/` (offline demo mode; `tests/test_e2e_demo.py` runs the whole pipeline with it). Try UI changes with `DEMO_MODE=true uv run idea-to-mvp`.
- After changing graph topology run `uv run python scripts/gen_graph_diagram.py`; a test enforces it.
- Docs are checked by `tests/test_docs_sync.py`: a path, test name (written `tests/<file>.py::<test>`), or setting named in backticks in a doc must exist, and the architecture doc's LangGraph patterns table must keep naming a real file and test per pattern.
- Ruff line length is 100; `uv run ruff check .`, `uv run mypy`, and `uv run pytest` must stay green.

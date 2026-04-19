# Changelog

All notable changes to this project will be documented in this file.

## [0.2.0] - 2026-04-19

### Workflow
- Added a planner follow-up after the architect step that asks whether to generate an execution pack.
- Added project blueprint generation in `project_blueprints/` with scoped `AGENTS.md` files and a task-oriented `plan.md`.
- Added Markdown export support so the visible session can be saved to `exports/` at any time.

### Model and prompt updates
- Fixed Gemini output budgeting by passing Google token limits at invocation time instead of relying only on model initialization.
- Tightened the architect prompt to reduce overly detailed responses and lower the risk of truncated architecture output.
- Simplified the planner `plan.md` prompt so it focuses on execution tasks, contracts, and tests instead of repeating project guidance.

### UX
- Added a `Save to Markdown` action in the UI.
- Improved planner and architect progress states so the UI can show more specific in-progress messages.

### Docs
- Refreshed `README.md` and contributor documentation to match the current discussion, architect, planner, and export flow.

## [0.1.0] - 2026-04-11

### Core capabilities
- Multi-agent idea discussion with PM, Tech Lead, and Skeptic roles.
- Streaming panel conversation in a Gradio chat interface.
- Automatic summary generation after discussion rounds.
- Automatic generation of five MVP decision questions.
- Architect follow-up mode that produces two implementation options.

### Model and provider support
- Configurable provider/model selection for each role (`openai`, `anthropic`, `google`).
- OpenAI fallback model support for empty/invalid primary outputs.
- Environment-based runtime configuration through `.env`.

### Project quality and maintainability
- Refactored architecture with a thin `app.py` entrypoint and separated service/render utilities.
- Shared text normalization utilities and stronger typed state contracts.
- Optional graph checkpointer toggle (`ENABLE_CHECKPOINTER`).
- Added test suite and CI for linting and regression checks.

### Open-source readiness
- Added `README.md`, `LICENSE`, `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`.
- Added `pyproject.toml` with project metadata and development tool settings.

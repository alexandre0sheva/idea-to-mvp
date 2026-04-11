# Changelog

All notable changes to this project will be documented in this file.

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

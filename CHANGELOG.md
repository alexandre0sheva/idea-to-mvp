# Changelog

All notable changes to this project will be documented in this file.

## Unreleased

### Added
- **`idea-to-mvp doctor`**: checks each role's API key and model with one tiny real call per provider/model (or `--offline` for configuration only), plus the bundled Claude Code CLI; exits non-zero on problems.
- **Resilience**: transient model errors (rate limits, timeouts, connection resets, 5xx) are retried with backoff at node level on top of the provider SDK's own retries; new `LLM_TIMEOUT_SECONDS` and `LLM_MAX_RETRIES` settings.
- **Usage tracking**: token usage per role accumulates in the run state and the running total shows in the UI status line.
- **Optional LangSmith tracing** via `LANGSMITH_*` in `.env`; runs are named and tagged with their session.
- **Durable sessions**: graph checkpoints are stored in SQLite (`CHECKPOINTER=sqlite`, `CHECKPOINT_DB`), so a run survives restarts. A *Saved sessions* panel lists, resumes, and deletes sessions; a run that stopped mid-step can be continued from its last checkpoint.
- **Demo mode** (`DEMO_MODE=true`): run the entire pipeline offline with canned, idea-aware model outputs and a stand-in implementation stage — no API keys, no spend. An end-to-end test drives every gate this way in CI.
- **Implementation stage**: after the blueprint is approved, Claude Agent SDK agents build the project in `generated_projects/` (gated behind an explicit confirmation with model + budget-cap warning; per-session `IMPLEMENTER_MAX_BUDGET_USD` cost cap).
- **Verification stage**: a verification agent runs the generated project's own test suite with a bounded fix loop (`MAX_FIX_ATTEMPTS`), followed by a delivery report with file tree and verdict.
- **Strategy agent**: auto-selects the execution mode — `subagents` (one lead session with per-workstream subagents) or `agent_team` (sequential focused sessions) — recorded in the blueprint's `STRATEGY.json`.
- **Architecture choice gate**: the pipeline pauses for the user to pick Option A or Option B before planning.
- **Blueprint v2**: packs now include `README.md`, `PRD.md` (numbered requirements), `ARCHITECTURE.md`, a `plan.md` with acceptance criteria and workstream ownership, and generated `.claude/agents/*.md` subagent definitions.
- **Pipeline stage tracker** in the UI showing progress through all nine stages.

### Changed
- **The UI is now a projection of graph state**: chat, mode, status, and progress are derived from the checkpoint after every update, replacing four parallel copies of state in the browser session. The graph runs on an async runtime (`astream`); exports are built from the same derived transcript. `ENABLE_CHECKPOINTER` is replaced by `CHECKPOINTER=sqlite|memory`.
- **Typed agent hand-offs**: the MVP questions, architecture options, and execution strategy are now schema-validated Pydantic outputs (`schemas.py`) instead of prose parsed with regexes/hand-rolled JSON. A malformed model reply gets one retry with the validation error, then a deterministic fallback. Each question now carries a reason it matters and a suggested answer (shown in the chat).
- The planner-offer LLM call is gone: the plan gate asks a fixed question, so a run makes one fewer model call.
- **Internal refactor**: the 800-line `agents.py` is split into `llm/` (provider construction, runtime, `invoke_text`) and one `nodes/` module per pipeline concern. Empty-output and truncation retries are now provider-agnostic in `llm.invoke_text`; the OpenAI-only retry path and the `OPENAI_FALLBACK_MODEL` setting are removed. A provider/model mismatch now logs a warning instead of silently rewriting the provider.
- **Package layout**: source moved into an installable `idea_to_mvp` package (`src/` layout); run with `uv run idea-to-mvp` or `python -m idea_to_mvp`. Dual relative/absolute imports are gone. Architecture docs moved to `docs/architecture.md`.
- **Output location**: exports, blueprint packs, and generated projects are now written under `OUTPUT_DIR` (default `~/idea-to-mvp/{exports,blueprints,projects}`) instead of inside the repository, so implementation agents never run next to `.env`. To keep existing data, move `exports/`, `project_blueprints/` (now `blueprints/`), and `generated_projects/` (now `projects/`) into the new location.
- **Tooling**: dependencies are declared once in `pyproject.toml` with a committed `uv.lock` (`requirements.txt` removed); setup, CI, and docs use `uv`. CI runs ruff, mypy, and pytest on Python 3.11–3.13.
- **Model defaults refreshed and validated** against current provider docs: `claude-sonnet-5-5` (summarizer/architect), `claude-sonnet-5-5` (Tech Lead), `claude-opus-5-5` (implementer), `gpt-5.6-terra` (PM), `gemini-3.8-flash` (Skeptic), `gpt-5.6-luna` (OpenAI fallback).
- `TEMPERATURE` is now only sent to models that accept sampling parameters (Claude 4.7+/5.x, GPT-5.x, and Gemini 3.x reject or discourage it) and can be left empty.
- Gradio 6 only: removed version-compatibility shims from `app.py`.
- Pipeline now runs as one continuous LangGraph thread with real `interrupt()` gates for answers and the plan decision (no more phase-flag routing or per-phase threads).
- All role prompts and provider/model wiring moved to a declarative registry in `roles.py`.
- Discussion prompts are round-aware: panelists know their round and must converge in the final round; the Skeptic must pair every objection with the cheapest resolving test.
- The summarizer now reports panel disagreements and resolutions; architect options anchor to the user's stated constraints.
- The plan gate is a structured choice (radio + optional notes) instead of free-text yes/no parsing.

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

# Changelog

All notable changes to this project will be documented in this file.

## Unreleased

### Added
- **Live implementation view**: an *Implementation* tab shows a task board (tasks move from pending through running to done or failed, each with its workstream, branch, cost, commit, summary, and `git diff --stat`), a live log of what the agents are doing (verification lanes included), and a running cost meter against the run budget. The view switches to that tab when building starts and back when it ends.
- **Artifacts panel**: the project's folder path with a copy button, downloads for the delivered project and the blueprint as zips, and a read-only file explorer with a viewer that cannot leave the project (no secret files, no git internals).
- **Delivery dashboard** replaces the markdown delivery report: a verdict card per verification lane, a table of how each requirement fared, cost / tokens / time, and how to run the project, with the iterate form right below.
- **Gate forms**: every gate has its own form instead of one shared textbox. The MVP questions are five labelled fields prefilled with the suggested answers (with a *Use all suggestions* button); the architecture options are two side-by-side cards with a *Recommended* badge and the rationale; the implement gate shows a summary (model, sandbox, permission mode, tasks, DAG width, budget, estimated sessions), a red banner for risky setups, and a parallelism selector (`Sequential | Parallel N`); the iterate gate is a feedback box with *Iterate* / *Finish*.
- **Editable blueprint**: the implement gate lets you read and edit `PRD.md`, `ARCHITECTURE.md`, and `plan.md` before anything is built. Edits are validated on save (a plan edited into a cycle, or citing unknown tasks or requirements, is refused with the list of problems), and starting is blocked while there are unsaved changes.
- **UI shell and design system**: a sticky header shows the pipeline stepper with the time each stage took and a token / $ badge; a Settings accordion holds rounds, panel mode, autopilot, and a read-only model profile; the chat fills the window height; four example ideas replace the pre-filled text. All colours are CSS variables with light and dark values, so both themes are readable (a test enforces no hard-coded colours and contrast targets).
- **Autopilot**: a switch that answers the MVP questions with their suggested answers, takes the architect's recommendation, and generates the pack without pausing. The implementation gate always asks (it spends money).
- **Stop button**: cancels a running step cooperatively; the session keeps its last checkpoint and offers Continue.
- **Iteration loop**: after a delivery you can ask for changes ("add X / fix Y") and get the next version through the same machinery: a change planner turns the request into new plan tasks (`I2-01`, ...) appended to the plan without touching finished ones, the agents build only those, verification runs again, and the result is tagged `v0.2`. Each version has its own whole-run budget; `MAX_ITERATIONS` (default 5) bounds how many versions a project can go through.
- **Delivery bundle**: every delivery writes a zip of the project (no dependencies, build output, `.git`, secrets, or symlinks) to `OUTPUT_DIR/deliveries/`, and a verified version is tagged `v0.<n>` in the project's git repository. The delivery report links both.
- **Verification in three parallel lanes**: tests, quality (lint/type/security checks plus a smoke probe of the documented run command), and requirements (every P0 requirement must be exercised by a test; uncovered ones are reported) run at the same time, each returning a structured report. A missing or malformed report now fails its lane with the reason instead of silently counting as FAIL. Failed rounds go through a fix step and back to verification as ordinary graph steps (visible in the live console and checkpointed), fed the merged failure list; lane and fix spend now counts against the whole-run budget, and the delivery report shows a verdict per lane.
- **Parallel task execution**: independent plan tasks now run at the same time, each in its own git worktree (`IMPLEMENTER_MAX_PARALLEL`, default 3; `1` restores one-at-a-time), and are merged back in task order. A merge conflict gets one bounded resolver session; if it cannot resolve it, only that task fails and its dependents are skipped. Every running task reserves the per-task cost cap, so the whole-run budget cannot be overshot. The implement gate shows how many tasks the plan has and how many can run in parallel, and the live view lists all running tasks.
- **Task-by-task implementation**: agents now build the plan one task at a time, each in a fresh session that is committed by the orchestrator (`T01: <title>`). A killed run resumes at the first unfinished task in the same workspace, the UI shows live per-task progress and a running cost, and a whole-run budget cap (`IMPLEMENTER_MAX_TOTAL_USD`, default $25) is shared by all sessions. Strategy mode `subagents` still uses one lead session.
- **Agent sandbox**: implementation agents now run with an OS sandbox for shell commands (Seatbelt / bubblewrap: writes only in the workspace, network only to `IMPLEMENTER_ALLOWED_DOMAINS`, credential directories unreadable), a tool-call guard that confines file tools to the workspace and refuses dangerous commands, a scrubbed environment (provider keys and other secrets are blanked for the agent), isolated settings, and no web tools. New settings `IMPLEMENTER_SANDBOX` (`auto`/`on`/`off`) and `IMPLEMENTER_ALLOWED_DOMAINS`; opt-in live smoke tests (`pytest -m live`). The threat model is in `SECURITY.md`.
- **Workspaces are git repositories**: the pack is copied without `node_modules`, `.venv`, `coverage`, and `dist`, with a `.gitignore` and a first `blueprint` commit; the orchestrator commits after implementation and after each fix attempt (with git hardened against planted hooks, filters, and config).
- The implement gate states the real sandbox status, permission mode, and allowed domains, and shows a red warning when the sandbox is off or the mode is `bypassPermissions`.
- **Machine-readable plan with validation**: the pack now contains `plan.json` (typed tasks with dependencies, requirement IDs, contracts, tests, and project commands) and `plan.md` rendered from it. The plan is checked for cycles, unknown dependencies/workstreams/contracts, requirement IDs missing from the PRD, and uncovered P0 requirements; problems get one automatic repair, and a deterministic fallback plan is used if the model never produces a valid one.
- **Blueprint critic and revise loop**: a reviewer model cross-checks PRD, architecture, plan, and subagent prompts; documents it flags as blockers are regenerated with its notes (`MAX_BLUEPRINT_REVISIONS`, default 1; `PLAN_MAX_TOKENS` sizes the plan call). Notes that remain are written to `REVIEW.md` and shown at the implement gate.
- **Blueprint pack generated as a dependency DAG**: the plan is now written from the actual PRD and architecture (so it cites the PRD's real requirement IDs), and each subagent definition from the actual plan. Documents are generated in three waves, concurrently within a wave, instead of ~12 calls one after another.
- **Parallel, moderated panel**: PM, Tech Lead, and Skeptic write their opening statements at the same time, then a moderator picks who speaks next and ends the debate early, with a visible reason, once it has converged (never before everyone has spoken twice; the round count stays the hard cap). `PANEL_MODE=round_robin` restores the fixed rotation; `MODERATOR_PROVIDER`/`MODERATOR_MODEL` pick the cheap moderator model and `LLM_MAX_CONCURRENCY` caps parallel model calls. With one round the panel is now three independent openings.
- **`idea-to-mvp doctor`**: checks each role's API key and model with one tiny real call per provider/model (or `--offline` for configuration only), plus the bundled Claude Code CLI; exits non-zero on problems.
- **Resilience**: transient model errors (rate limits, timeouts, connection resets, 5xx) are retried with backoff at node level on top of the provider SDK's own retries; new `LLM_TIMEOUT_SECONDS` and `LLM_MAX_RETRIES` settings.
- **Usage tracking**: token usage per role accumulates in the run state and the running total shows in the UI status line.
- **Optional LangSmith tracing** via `LANGSMITH_*` in `.env`; runs are named and tagged with their session.
- **Durable sessions**: graph checkpoints are stored in SQLite (`CHECKPOINTER=sqlite`, `CHECKPOINT_DB`), so a run survives restarts. A *Saved sessions* panel lists, resumes, and deletes sessions; a run that stopped mid-step can be continued from its last checkpoint.
- **Demo mode** (`DEMO_MODE=true`): run the entire pipeline offline with canned, idea-aware model outputs and a stand-in implementation stage — no API keys, no spend. An end-to-end test drives every gate this way in CI.
- **Implementation stage**: after the blueprint is approved, Claude Agent SDK agents build the project in `generated_projects/` (gated behind an explicit confirmation with model + budget-cap warning; per-session `IMPLEMENTER_MAX_BUDGET_USD` cost cap).
- **Verification stage**: verification lanes run the generated project's own test suite with a bounded fix loop (`MAX_FIX_ATTEMPTS`), followed by a delivery report with file tree and verdict.
- **Strategy agent**: auto-selects the execution mode — `subagents` (one lead session with per-workstream subagents) or `agent_team` (sequential focused sessions) — recorded in the blueprint's `STRATEGY.json`.
- **Architecture choice gate**: the pipeline pauses for the user to pick Option A or Option B before planning.
- **Blueprint v2**: packs now include `README.md`, `PRD.md` (numbered requirements), `ARCHITECTURE.md`, a `plan.md` with acceptance criteria and workstream ownership, and generated `.claude/agents/*.md` subagent definitions.
- **Pipeline stage tracker** in the UI showing progress through all nine stages.

### Changed
- Strategy mode `agent_team` now means one session per plan task, parallel where the plan allows (it used to be one session per workstream, one after another); the old value is still accepted.
- `IMPLEMENTER_MAX_BUDGET_USD` is now `IMPLEMENTER_MAX_TASK_USD` (default $5, per task session; the old name still works); new `IMPLEMENTER_MAX_TASK_TURNS` (default 40). Workspace creation is its own graph step (`prepare_workspace`), and the implementer node is async.
- **Safer default permission mode**: `IMPLEMENTER_PERMISSION_MODE` now defaults to `acceptEdits` (was `bypassPermissions`). An existing `.env` that still sets `bypassPermissions` keeps the old, unattended behaviour; check yours. The README and `.env.example` no longer claim that `cwd` confines the agents.
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

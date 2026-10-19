# Changelog

All notable changes to this project will be documented in this file.

## Unreleased

### Models
- **One model per kind of agent session**: `IMPLEMENTER_MODEL` now runs only the task sessions; the new `VERIFIER_MODEL` runs the three verification lanes, and the new `ESCALATION_MODEL` runs the hard cases (the merge-conflict resolver and every fix attempt after the first). `MODEL_PROFILE` covers all three, and the implement gate lists each one. Usage records name the model each session actually ran on.
- **Re-balanced profiles** (checked against the providers' model and pricing pages on 2026-10-19; reasons in the *Model choices* table of `docs/architecture.md`). `balanced`: the architect, and with it strategy, plan writer, change planner, and blueprint critic, moves from Sonnet 5.5 to Opus 5.5; agent task sessions and verification lanes move from Opus 5.5 to Sonnet 5.5, and escalated sessions stay on Opus 5.5. A typical run is estimated at $3.43, down from $4.74 for the old defaults on the same run. `fast`: Haiku 5.5 for every agent session (escalating to Sonnet 5.5) and `gemini-3.8-flash` for the Skeptic, about $0.17. `quality`: Opus 5.5 for every agent session and Fable 5.1 (`claude-fable-5-1`) for escalated ones, about $5.47. New catalog entries: `claude-fable-5-1`, `gpt-6-astra`, and `gemini-4-argon` (not in a profile yet). The settings accordion and the README show each profile's typical-run estimate.
- *Upgrade note:* `.env.example` no longer sets `IMPLEMENTER_MODEL`. An `.env` copied from an earlier version still does, and it keeps running every agent session (tasks, lanes, fixes, resolver) on that one model, as before. Remove the `IMPLEMENTER_MODEL` line to get the profile's split, or set `VERIFIER_MODEL` / `ESCALATION_MODEL` as well. A pinned `ARCHITECT_MODEL` likewise keeps the architect off the new Opus default.
- **Model catalog**: `src/idea_to_mvp/models.toml` is now the one source of truth for model knowledge: for every model its provider, id and aliases, tier (`fast` / `balanced` / `frontier`), status, $ per million tokens (input / output / cached input), context window, max output, structured-output / sampling / web-search support, the date it was last checked, and a source link. The model profiles (the choice per role of `fast` / `balanced` / `quality`) and the "accepts `temperature`" rule read from it. A model that is not listed still works and falls back to the old name pattern for sampling.
- **`idea-to-mvp models`** prints the model of every role for each profile, the price list, and the estimated cost of a typical run per profile and for your configuration (planning calls plus agent sessions). `--check` is opt-in and makes one live call per provider with a key to list its models, then flags configured models the provider does not list, ones the catalog marks deprecated, and ones with a newer release of the same family (exit code 1 for unknown or deprecated).
- **Newer default models** (checked against the providers' pages on 2026-10-19): the PM is `gpt-6.1-sol` (`gpt-6-luna` in the `fast` profile) instead of `gpt-5.6-terra`, and the moderator and the whole `fast` profile use `claude-haiku-5-5` instead of `claude-haiku-4-5-20251001` (the `gpt-5.4-mini`, `gpt-5.6-terra`, and Haiku 4.5 entries are gone from the catalog). `claude-sonnet-5-5`, `claude-opus-5-5`, and `gemini-3.8-flash` stay. *Upgrade note:* a `.env` that pins `PM_MODEL`, `MODERATOR_MODEL`, or any other `*_MODEL` / `*_PROVIDER` keeps the pinned model; remove those lines to get the new defaults. `idea-to-mvp doctor` warns about pinned models the catalog does not know.
- `idea-to-mvp doctor` now warns, offline, about a configured model that is not in the catalog, has no price, or is deprecated. The settings accordion shows each model's price per million tokens.

### Cost
- **Every model call is priced.** The panel, summary, architecture, blueprint, critic, and research calls now carry a dollar cost from the model catalog (cached input at the cached rate), so the header shows **planning spend and agent spend as separate badges** and the delivery dashboard has a *Planning cost* figure next to *Agent cost*. A model with no price is shown as "unpriced" rather than left out of the total silently; a demo run costs $0.00. `MODEL_PRICES_FILE` points at a TOML file of per-model prices (`[prices."<model id>"]` with `input_per_mtok`, `output_per_mtok`, optional `cached_input_per_mtok`) for negotiated or in-house prices; `idea-to-mvp doctor` warns when it cannot be read.
- **A dollar range at the implement gate.** The gate shows the estimated cost of the run (tasks x the cost of a task session, one verification round, and fix attempts in the high end, capped by the whole-run budget): the median of earlier projects' finished tasks on the same model when there are enough, else the catalog's typical session. The delivery dashboard sets the estimate against what the first version's agents actually spent.
- **Resolver and lead-session spend are counted.** A merge resolver's cost is now its own `resolver` usage record on the model it ran on (it used to be folded into its task's record), and the lead session of strategy `subagents` is counted in usage, so it now counts against the whole-run budget (`IMPLEMENTER_MAX_TOTAL_USD`) like every other session.

### Observability
- **Agent trajectories and a run ledger.** Every agent session (task, merge resolver, verification lane, fix, and the lead / workstream sessions) now writes its stream to `<workspace>/.idea-to-mvp/trajectories/<label>.md` (tool calls, truncated results, the agent's text, errors, and a final result line with cost and turns) and adds one row to `<workspace>/.idea-to-mvp/ledger.json` (kind, label, iteration, attempt, model, cost, turns, outcome, session id, times). The Implementation console is rebuilt from these files after a restart instead of coming back empty, a task card names its trajectory file (open it in the file explorer), and the delivery step logs a warning when the ledger's total differs from the agent spend in the usage record. Both files stay out of the delivery zip and off limits to agents.

### Headless CLI
- **`idea-to-mvp run "<idea>"`** drives the whole pipeline without the UI, on the same graph, checkpoints, and session list (a headless session can be opened in the UI and the other way round). Gates are answered by flags: `--answers FILE` (one answer per line), `--arch 1|2`, `--autopilot` (the suggested answers, the recommended architecture, and the blueprint pack), and `--implement`. **Agent sessions start only with an explicit `--implement`**, autopilot or not; without it the run stops at the implement gate and prints the dollar estimate. `--max-total-usd N` sets the whole-run agent budget for this run, `--parallel N` how many tasks are built at once. Progress goes to stderr; the summary (outcome, workspace, delivery zip, cost, the next command) goes to stdout, or as JSON with `--json`. Exit codes: 0 delivered and verified, 2 delivered with failures, 3 stopped at a gate, 1 error (usage errors exit 1 as well).
- **`idea-to-mvp resume <session>`** carries a saved session on from the gate it waits at, or from the step it was stopped in, with the same flags; at the iterate gate `--feedback "..." --implement` builds the next version. **`idea-to-mvp sessions`** (`--json`) lists the saved sessions with the gate each one waits at.

### Verification
- **Deterministic test evidence.** The plan gains `commands.test_report`: the test command written to also leave JUnit XML (and coverage) in `.idea-to-mvp/reports/`, filled per stack by the plan writer. The tests lane runs it, and the orchestrator reads the files itself instead of trusting the agent's `passed`: a missing report, zero tests, only skipped tests, or any failed test fails the lane with the parsed reason, and the failing test names reach the fix session. The dashboard's Tests card shows real passed / failed / skipped counts and coverage. Plans written before this keep the agent's verdict, marked "agent-reported". Reports are evidence, not proof (see `SECURITY.md`).

### Prompt hardening
- **Evidence boundary.** The verification lanes, the fix and merge-resolver sessions, the blueprint critic, and the change planner are told that files, code, test output, and web text are data: they never follow instructions found there and never run a script because a file says so. A test fails for any judge prompt without the block.
- **Answer-free implementers.** Task and fix sessions work from their brief and the project's own tests and do not look outside the workspace for answers.
- **Ordered rubric.** The blueprint critic and the requirements lane weigh findings in a fixed order (delivery, conformance, fidelity, consistency, operational fitness, finish), and length is not merit.
- **"No change" is an answer.** A blueprint revision can leave a document as it is when no review issue holds. If the product already does what an iterate request asks, the change planner now says so (a `reason` naming what covers it) instead of inventing tasks: the run ends at the delivered version, with no new version, and the chat shows the reason.
- Ideas from Google Research's Harness² (arXiv:2610.14224); the prompts are our own.

### Dependencies
- Updated to the newest versions the `pyproject.toml` ranges allow (supersedes Dependabot PR #4): gradio 6.30.0, langgraph 1.2.16, langchain-core 1.6.12, langchain-openai 1.7.2, langchain-anthropic 1.7.6, langchain-google-genai 4.4.2, claude-agent-sdk 0.2.168, pydantic-settings 2.16.0, python-dotenv 1.2.4, langgraph-checkpoint-sqlite 3.1.2, markdown 3.11.1; dev tools ruff 0.17.1 and mypy 2.4.0. No code changes were needed; the `ruff-pre-commit` hook is pinned to v0.17.1 to match.

## [0.3.0] - 2026-09-30

The pipeline is now one durable, parallel, sandboxed LangGraph run with a rebuilt interface. Changes that affect an existing setup are listed under *Upgrade notes*.

### Pipeline and agents
- **Parallel, moderated panel**: PM, Tech Lead, and Skeptic write their opening statements at the same time, then a moderator picks who speaks next and ends the debate early, with a visible reason, once it has converged (never before everyone has spoken twice; the round count stays the hard cap). `PANEL_MODE=round_robin` restores the fixed rotation; `MODERATOR_PROVIDER` / `MODERATOR_MODEL` pick the cheap moderator model and `LLM_MAX_CONCURRENCY` caps parallel model calls. Discussion prompts are round-aware, and the Skeptic pairs every objection with the cheapest test that would resolve it.
- **Grounded research step** (optional, `ENABLE_RESEARCH=true`, off by default): before the panel starts, a researcher model searches the web with its provider's own search tool (Anthropic web search, OpenAI web search, or Gemini Google Search grounding; `RESEARCH_PROVIDER` / `RESEARCH_MODEL` / `RESEARCH_MAX_SEARCHES`) and a short cited brief (real competitors with links, market notes, gaps) is given to the panel and shown as a card in the chat and in the session export. Web text is treated as untrusted data: competitors without a URL are dropped, only http(s) links are shown, and a failed search never stops the run.
- **Project preferences**: a collapsed accordion lets you state platform (web / mobile / CLI / API), stack hints, deploy target, and must-use / must-avoid items before the run. They are added to the panel, summary, architecture, strategy, and every blueprint prompt; must use / must avoid are hard constraints for every option and document.
- **Typed agent hand-offs**: the MVP questions (each with why it matters and a suggested answer), the architecture options (with a recommendation and rationale), and the execution strategy are schema-validated outputs instead of prose parsed with regexes. A malformed model reply gets one retry with the validation error, then a deterministic fallback. The planner-offer model call is gone (the plan gate asks a fixed question).
- **Strategy agent** chooses `subagents` (one lead session with per-workstream subagents) or `agent_team` (one session per plan task, in parallel where the plan allows) and records it in the blueprint's `STRATEGY.json`.
- **Blueprint pack as a dependency DAG**: documents are generated in three waves, concurrently within a wave, from the real upstream documents (the plan cites the PRD's actual requirement IDs; each subagent definition is written from the actual plan). The pack contains `README.md`, `PRD.md`, `ARCHITECTURE.md`, `AGENTS.md` guides, `plan.json` with `plan.md` rendered from it, `.claude/agents/*.md`, and `STRATEGY.json`.
- **Machine-readable plan with validation**: typed tasks with dependencies, requirement IDs, contracts, tests, and project commands, checked for cycles, unknown dependencies, workstreams and contracts, requirement IDs missing from the PRD, and uncovered P0 requirements. Problems get one automatic repair; a deterministic fallback plan is used if the model never produces a valid one.
- **Blueprint critic and revise loop**: a reviewer model cross-checks PRD, architecture, plan, and subagent prompts; documents it flags as blockers are regenerated with its notes (`MAX_BLUEPRINT_REVISIONS`, default 1; `PLAN_MAX_TOKENS` sizes the plan call). Remaining notes go to `REVIEW.md` and are shown at the implement gate.
- **Editable blueprint**: the implement gate lets you read and edit `PRD.md`, `ARCHITECTURE.md`, and `plan.md` before anything is built. Edits are validated on save (a plan edited into a cycle, or citing unknown tasks or requirements, is refused with the list of problems), and starting is blocked while there are unsaved changes.

### Implementation and safety
- **Task-by-task implementation**: one fresh Claude Agent SDK session per plan task, committed by the orchestrator (`T01: <title>`). A killed run resumes at the first unfinished task in the same workspace, and a whole-run budget cap (`IMPLEMENTER_MAX_TOTAL_USD`, default $25) is shared by all sessions, with `IMPLEMENTER_MAX_TASK_USD` and `IMPLEMENTER_MAX_TASK_TURNS` bounding one session.
- **Parallel task execution**: independent tasks run at the same time, each in its own git worktree (`IMPLEMENTER_MAX_PARALLEL`, default 3; `1` for one at a time; the implement gate can override it per run), and are merged back in task order. A merge conflict gets one bounded resolver session; if it cannot resolve it, only that task fails and its dependents are skipped. Every running task reserves the per-task cap, so the whole-run budget cannot be overshot.
- **Agent sandbox**: shell commands run in an OS sandbox (Seatbelt / bubblewrap: writes only in the workspace, network only to `IMPLEMENTER_ALLOWED_DOMAINS`, credential directories unreadable), behind a tool-call guard that confines file tools to the workspace and refuses dangerous commands, with a scrubbed environment (provider keys and other secrets are blanked), isolated settings, and no web tools (`IMPLEMENTER_SANDBOX`: `auto` / `on` / `off`). The threat model is in `SECURITY.md`; opt-in live smoke tests run with `pytest -m live`.
- **Workspaces are git repositories**: the pack is copied without `node_modules`, `.venv`, `coverage`, and `dist`, with a `.gitignore` and a first `blueprint` commit; the orchestrator commits after implementation and after each fix attempt, with git hardened against planted hooks, filters, and config.
- **An honest implement gate**: it states the real sandbox status, permission mode, allowed domains, model, tasks, DAG width, budget, and estimated sessions, and shows a red warning when the sandbox is off or the mode is `bypassPermissions`.

### Verification and delivery
- **Three parallel verification lanes**: tests, quality (lint/type/security checks plus a smoke probe of the documented run command), and requirements (every P0 requirement must be exercised by a test) each return a structured report; a missing or malformed report fails its lane with the reason. Failed rounds go through a checkpointed fix step and back to verification (`MAX_FIX_ATTEMPTS`), fed the merged failure list; lane and fix spend counts against the whole-run budget.
- **Delivery bundle**: every delivery writes a zip of the project (no dependencies, build output, `.git`, secrets, or symlinks) to `OUTPUT_DIR/deliveries/`, and a verified version is tagged `v0.<n>` in the project's git repository.
- **Iteration loop**: after a delivery you can ask for changes and get the next version through the same machinery: a change planner turns the request into new plan tasks (`I2-01`, ...) appended without touching finished ones, only those are built, verification runs again, and the result is tagged `v0.2`. Each version has its own whole-run budget; `MAX_ITERATIONS` (default 5) bounds how many versions a project can go through.

### Interface
- **The UI is a projection of graph state**: chat, mode, status, and progress are derived from the checkpoint after every update, so a session can be rebuilt after a restart and no copy of pipeline state can drift.
- **Gate forms**: every gate has its own form. The MVP questions are five labelled fields prefilled with the suggested answers (with *Use all suggestions*); the architecture options are two side-by-side cards with a *Recommended* badge; the implement gate is a summary table with a parallelism selector; the iterate gate is a feedback box.
- **Live panel**: panel text streams token by token; the three opening statements show as one row of cards, the moderator's convergence note is a slim banner, and turns older than the last round are collapsed.
- **Live implementation view**: an *Implementation* tab with a task board (pending, running, done, failed, each with workstream, branch, cost, commit, summary, and `git diff --stat`), a live log of what the agents do, and a running cost meter against the run budget. An artifacts panel offers the project folder with a copy button, zip downloads of the project and the blueprint, and a read-only file viewer that cannot leave the project.
- **Delivery dashboard**: a verdict card per verification lane, a table of how each requirement fared, cost / tokens / time, and how to run the project, with the iterate form right below.
- **UI shell**: a sticky stepper shows the time each stage took and a token / $ badge; a Settings accordion holds rounds, panel mode, autopilot, and the model profile with the models it resolves to; the chat fills the window height; four example ideas replace the pre-filled text; all colours are CSS variables with light and dark values (a test enforces no hard-coded colours).
- **Autopilot** answers the questions with their suggestions, takes the recommended architecture, and generates the pack; the implementation gate always asks, because it spends money. **Stop** cancels a running step and the session keeps its last checkpoint and offers Continue.
- **Sessions and export**: a *Saved sessions* panel lists, resumes, and deletes sessions. *Save session* writes the whole run (panel, summary, questions, answers, architecture, strategy, blueprint files, implementation log, verification lanes, delivery report, your decisions) as Markdown and as JSON.

### Configuration and operations
- **Model profiles**: `MODEL_PROFILE=fast|balanced|quality` picks the models of the panel, summarizer, architect, moderator, and researcher in one setting. A role whose `*_PROVIDER` or `*_MODEL` is set explicitly keeps it; the per-role lines in `.env.example` are commented out so a copied file does not pin them.
- **Durable sessions**: graph checkpoints are stored in SQLite (`CHECKPOINTER=sqlite|memory`, `CHECKPOINT_DB`), so a run survives restarts.
- **Demo mode** (`DEMO_MODE=true`): the whole pipeline offline with canned, idea-aware model outputs and a stand-in implementation stage: no API keys, no spend. An end-to-end test drives every gate this way in CI.
- **`idea-to-mvp doctor`** checks each role's API key and model with one tiny real call per provider and model (`--offline` checks configuration only), plus the bundled Claude Code CLI; it exits non-zero on problems.
- **Resilience and observability**: transient model errors (rate limits, timeouts, connection resets, 5xx) are retried with backoff at node level on top of the provider SDK's own retries (`LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES`); token usage accumulates per role and shows in the UI; optional LangSmith tracing via `LANGSMITH_*` names and tags each run with its session.
- **Model defaults refreshed** against current provider docs (`claude-sonnet-5-5`, `claude-opus-5-5` for the implementer, `gpt-5.6-terra`, `gemini-3.8-flash`); `TEMPERATURE` is only sent to models that accept sampling parameters and can be left empty.

### Developer experience
- **Package layout**: an installable `idea_to_mvp` package (`src/` layout; `uv run idea-to-mvp` or `python -m idea_to_mvp`), with the old 800-line `agents.py` split into `llm/` and one `nodes/` module per concern, absolute imports only, and all role prompts and provider wiring in a declarative registry (`roles.py`).
- **Tooling and gates**: dependencies are declared once in `pyproject.toml` with a committed `uv.lock`. CI runs ruff, mypy, and pytest on Python 3.11 to 3.13 and fails on coverage below 80%, a stale `uv.lock`, a graph diagram that is out of sync, or a golden blueprint pack that stops validating (three golden ideas run through the whole pipeline offline). Pre-commit (ruff, mypy), Dependabot for `uv` and GitHub Actions, stricter lint rules, and strict type checking for `llm/`, `schemas.py`, and `plan.py` are in place.
- **Blueprint eval**: `scripts/eval_blueprint.py --live` scores a generated pack with an LLM judge on requirement coverage, contract consistency, and test specificity (opt-in, never run by CI).
- **Docs**: `docs/architecture.md` is the single home for the design, including a table that maps every LangGraph pattern used to its code and a test, and a guide to adding an agent or a gate; a test checks that the paths, tests, and settings the docs name exist.

### Upgrade notes
- **Output location**: exports, blueprint packs, and generated projects are written under `OUTPUT_DIR` (default `~/idea-to-mvp/{exports,blueprints,projects}`) instead of inside the repository, so implementation agents never run next to `.env`. To keep existing data, move `exports/`, `project_blueprints/` (now `blueprints/`), and `generated_projects/` (now `projects/`) there.
- **Permission mode**: `IMPLEMENTER_PERMISSION_MODE` now defaults to `acceptEdits` (was `bypassPermissions`). An existing `.env` that still sets `bypassPermissions` keeps the old, unattended behaviour; check yours.
- **Renamed or removed settings**: `IMPLEMENTER_MAX_BUDGET_USD` is now `IMPLEMENTER_MAX_TASK_USD` (default $5; the old name still works); `ENABLE_CHECKPOINTER` is replaced by `CHECKPOINTER=sqlite|memory`; `OPENAI_FALLBACK_MODEL` is gone (empty-output and truncation retries are provider-agnostic in `llm.invoke_text`).
- **Pinned models**: a role whose `*_PROVIDER` or `*_MODEL` is set in your `.env` ignores `MODEL_PROFILE`; remove those lines to let the profile choose.
- **`agent_team`** now means one session per plan task, in parallel where the plan allows (it used to be one session per workstream, one after another).
- **Tooling**: `requirements.txt` is removed; use `uv sync`. The *Save to Markdown* button is now *Save session*. The UI needs Gradio 6.

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

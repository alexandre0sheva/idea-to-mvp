# Architecture

`idea-to-mvp` is a Gradio + LangGraph app that takes a product idea through a structured multi-agent pipeline and produces a built, tested first version. This file is the single home for how it is put together; setup lives in `CONTRIBUTING.md`, settings in `.env.example`, and the threat model in `SECURITY.md`.

## Pipeline

1. **Panel** — three agents (PM, Tech Lead, Skeptic) write their opening statements in parallel, then a moderator picks who speaks next and ends the debate early once it has converged, within a turn budget of `rounds × 3` (`PANEL_MODE=round_robin` keeps the fixed rotation).
2. **Summarizer** — executive brief plus exactly 5 clarifying MVP questions.
3. **Answers gate** — the graph pauses at `collect_answers` (`interrupt()`); the user's answers resume it. Each question carries a reason it matters and a suggested answer.
4. **Architect + architecture gate** — two options; the graph pauses at `arch_choice` for a structured `{option, notes}` decision.
5. **Strategy** — emits a validated `ExecutionStrategy` `{mode, reasoning, workstreams}` choosing `subagents` vs `agent_team` (deterministic fallback strategy if the model output stays invalid).
6. **Plan gate + blueprint** — pause at `plan_gate` (static question); `{generate, notes}` resumes and writes the blueprint pack in three dependency waves (PRD and architecture first, then the plan and guides written from them, then the subagents written from the plan).
7. **Implementation gate + implementer** — pause at `implement_gate` with a cost warning and a truthful statement of the sandbox status, permission mode, and allowed domains (a red warning card when the sandbox is off or the mode is `bypassPermissions`); `{implement, notes}` resumes. the `prepare_workspace` node copies the pack into the projects directory as a git repository (its own checkpointed step, so a continued run reuses the same workspace), then it is built: one fresh Claude Agent SDK session per plan task for strategy mode `agent_team`, run in parallel git worktrees by the `implement_plan` subgraph (or one at a time by `implementer` when `IMPLEMENTER_MAX_PARALLEL=1`; see *The implementation engine*), or one lead session with subagents for `subagents`; sessions are configured by `implementation/options.py` and the orchestrator commits.
8. **Verification + delivery report** — three read-mostly lanes (tests, quality, requirements) run in parallel and each returns a structured `LaneReport`; a `verdict` node folds them, failed rounds go to a `fix` node and back to `verify` (a bounded, checkpointed loop; see *Verification lanes*), then a deterministic delivery report (with the delivery zip and a `v0.<n>` git tag when verification passed) follows.
9. **Iterate gate** — after every delivery the graph pauses at `iterate_gate` with `{iterate, feedback}`: changes loop through `change_planner` back into the same engine for the next version; finishing (or reaching `MAX_ITERATIONS`) ends the run (see *Iteration loop*).

The graph below is generated from the real `StateGraph` (`uv run python scripts/gen_graph_diagram.py`; a test fails if it drifts).

<!-- graph:start -->
```mermaid
---
config:
  flowchart:
    curve: linear
---
graph TD;
	__start__([<p>__start__</p>]):::first
	summarizer(summarizer)
	collect_answers(collect_answers)
	architect(architect)
	arch_choice(arch_choice)
	strategy(strategy)
	plan_gate(plan_gate)
	implement_gate(implement_gate)
	prepare_workspace(prepare_workspace)
	implementer(implementer)
	verify(verify)
	verify_lane(verify_lane)
	verdict(verdict)
	fix(fix)
	delivery_report(delivery_report)
	iterate_gate(iterate_gate)
	change_planner(change_planner)
	__end__([<p>__end__</p>]):::last
	__start__ --> panel\3a__start__;
	arch_choice --> strategy;
	architect --> arch_choice;
	change_planner -.-> implement_plan\3aprepare;
	change_planner -.-> implementer;
	collect_answers --> architect;
	delivery_report -.-> __end__;
	delivery_report -.-> iterate_gate;
	fix --> verify;
	implement_gate -.-> __end__;
	implement_gate -.-> prepare_workspace;
	implement_plan\3afinish --> verify;
	implementer --> verify;
	iterate_gate -.-> __end__;
	iterate_gate -.-> change_planner;
	panel\3a__end__ --> summarizer;
	plan_bundle\3awrite --> implement_gate;
	plan_gate -.-> __end__;
	plan_gate -.-> plan_bundle\3awave_1;
	prepare_workspace -.-> implement_plan\3aprepare;
	prepare_workspace -.-> implementer;
	strategy --> plan_gate;
	summarizer --> collect_answers;
	verdict -. &nbsp;exhausted&nbsp; .-> delivery_report;
	verdict -. &nbsp;retry&nbsp; .-> fix;
	verify -.-> delivery_report;
	verify -.-> verify_lane;
	verify_lane --> verdict;
	subgraph panel
	panel\3a__start__(<p>__start__</p>)
	panel\3aopening_turn(opening_turn)
	panel\3amerge_openings(merge_openings)
	panel\3amoderator(moderator)
	panel\3aspeaker_turn(speaker_turn)
	panel\3a__end__(<p>__end__</p>)
	panel\3a__start__ -.-> panel\3aopening_turn;
	panel\3a__start__ -.-> panel\3aspeaker_turn;
	panel\3amerge_openings -.-> panel\3a__end__;
	panel\3amerge_openings -.-> panel\3amoderator;
	panel\3amoderator -.-> panel\3a__end__;
	panel\3amoderator -.-> panel\3aspeaker_turn;
	panel\3aopening_turn --> panel\3amerge_openings;
	panel\3aspeaker_turn -.-> panel\3a__end__;
	panel\3aspeaker_turn -.-> panel\3amoderator;
	panel\3aspeaker_turn -.-> panel\3aspeaker_turn;
	end
	subgraph plan_bundle
	plan_bundle\3awave_1(wave_1)
	plan_bundle\3adoc_1(doc_1)
	plan_bundle\3awave_2(wave_2)
	plan_bundle\3adoc_2(doc_2)
	plan_bundle\3awave_3(wave_3)
	plan_bundle\3adoc_3(doc_3)
	plan_bundle\3aplan_writer(plan_writer)
	plan_bundle\3areview(review)
	plan_bundle\3arevise(revise)
	plan_bundle\3arewrite_doc(rewrite_doc)
	plan_bundle\3arewrite_plan(rewrite_plan)
	plan_bundle\3awrite(write)
	plan_bundle\3adoc_1 --> plan_bundle\3awave_2;
	plan_bundle\3adoc_2 --> plan_bundle\3awave_3;
	plan_bundle\3adoc_3 --> plan_bundle\3areview;
	plan_bundle\3aplan_writer --> plan_bundle\3awave_3;
	plan_bundle\3areview -.-> plan_bundle\3arevise;
	plan_bundle\3areview -.-> plan_bundle\3awrite;
	plan_bundle\3arevise -.-> plan_bundle\3arewrite_doc;
	plan_bundle\3arevise -.-> plan_bundle\3arewrite_plan;
	plan_bundle\3arewrite_doc --> plan_bundle\3areview;
	plan_bundle\3arewrite_plan --> plan_bundle\3areview;
	plan_bundle\3awave_1 -.-> plan_bundle\3adoc_1;
	plan_bundle\3awave_1 -.-> plan_bundle\3awave_2;
	plan_bundle\3awave_2 -.-> plan_bundle\3adoc_2;
	plan_bundle\3awave_2 -.-> plan_bundle\3aplan_writer;
	plan_bundle\3awave_2 -.-> plan_bundle\3awave_3;
	plan_bundle\3awave_3 -.-> plan_bundle\3adoc_3;
	plan_bundle\3awave_3 -.-> plan_bundle\3areview;
	end
	subgraph implement_plan
	implement_plan\3aprepare(prepare)
	implement_plan\3apick_wave(pick_wave)
	implement_plan\3arun_task_node(run_task_node)
	implement_plan\3amerge_wave(merge_wave)
	implement_plan\3afinish(finish)
	implement_plan\3amerge_wave --> implement_plan\3apick_wave;
	implement_plan\3apick_wave -.-> implement_plan\3afinish;
	implement_plan\3apick_wave -.-> implement_plan\3arun_task_node;
	implement_plan\3aprepare --> implement_plan\3apick_wave;
	implement_plan\3arun_task_node --> implement_plan\3amerge_wave;
	end
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```
<!-- graph:end -->

`panel` is a subgraph (see *The panel* below); `plan_gate` and `implement_gate` can end the run early.

The graph runs on a single checkpointer thread per session; the UI resumes interrupts with `Command(resume=...)`. There is no `phase` field — UI mode is derived from the interrupt the graph is paused at.

## Modules

Everything lives in the `idea_to_mvp` package under `src/`.

| Module | Role |
|---|---|
| `config.py` | Pydantic `Settings` loaded from `.env`; `get_settings()`; output directories (`exports_dir`, `blueprints_dir`, `projects_dir` under `output_dir`) |
| `state.py` | `IdeaDiscussionState` TypedDict — the single shared state contract — and `make_initial_state()` |
| `graph.py` | `build_graph(checkpointer)`: nodes and conditional routing; `open_graph()` (SQLite/memory checkpointer lifecycle), `GraphProvider` (lazy, loop-safe handle for the UI), `pending_interrupt()` |
| `resilience.py` | `LLM_RETRY`: LangGraph retry policy (3 attempts, backoff) for transient errors only (`is_transient`) |
| `usage.py` | `UsageRecord`, the `@with_usage(role=...)` node decorator, `summarize_usage` |
| `observability.py` | `apply_tracing_env()`: exports only `LANGSMITH_*`/`LANGCHAIN_*` from `.env` |
| `doctor.py`, `cli.py` | `idea-to-mvp doctor` (keys, models, agent CLI) and the command-line entry point |
| `sessions.py` | `SessionRegistry`: the list of resumable sessions (title, stage, gate) next to the checkpoints |
| `nodes/` | One module per pipeline concern: `panel` (the panel subgraph), `discussion` (one speaker turn), `blueprint_graph` (the blueprint subgraph) with its node functions in `blueprint`, `summary`, `architecture`, `strategy`, `gates` (all `interrupt()` gates and their routers), `blueprint`, `implement`, `verify` (the `verify` / `verify_lane` / `verdict` / `fix` steps), `report`, `iterate` (the iterate gate and `change_planner`); `common` holds helpers shared with the UI |
| `llm/` | Model access: `providers.py` (vendor SDK construction and quirks), `runtime.py` (`get_runtime(role_key)`), `invoke.py` (`invoke_text`), `structured.py` (`invoke_structured`) |
| `schemas.py` | Pydantic models exchanged between agents (`ModeratorDecision`, `QuestionSet`, `ArchitectureProposal`, `ExecutionStrategy`) and their markdown renderers |
| `roles.py` | `RoleSpec` registry: system prompts and provider/model/token wiring per agent |
| `blueprints.py` | Blueprint document specs (prompt, dependency `wave`, upstream `needs`), `prompt_for()`, and `write_bundle()` (LLM-free) |
| `plan.py` | The typed execution plan (`Plan`, `PlanTask`, contracts, commands), `validate_plan`, `execution_waves`, `render_plan_markdown`, `fallback_plan`, `parse_plan_markdown` (an edited `plan.md` back into a `Plan`); iterations: `ChangePlan`, `append_iteration_tasks`, `iteration_of`, `fallback_change_tasks` (LLM-free) |
| `delivery.py` | `make_delivery_zip` (project zip without dependencies, secrets, symlinks) and `tag_iteration` (`v0.<n>` git tag); LLM-free |
| `blueprint_review.py` | `CritiqueReport`/`Issue` and the pure helpers that pick revision targets and render `REVIEW.md` (LLM-free) |
| `implementer.py` | Claude Agent SDK sessions: the lead/team implementation modes and `iter_agent_messages`, the one place a session is streamed (options and workspaces come from `implementation/`) |
| `implementation/` | `executor.py` (task-by-task engine, `BudgetTracker`, conflict resolver), `scheduler.py` (ready tasks, blocked tasks, DAG width), `merge.py` (worktrees, task commits, merges), `events.py` (live `ImplEvent`s from SDK messages), `progress.py` (resumable per-task results), `options.py` (`build_agent_options`: the one place that sets cwd, model, budget, OS sandbox, guard hook, scrubbed env, settings isolation), `guard.py` (`PreToolUse` denylist), `verify.py` (the three verification lanes, `LaneReport` parsing and merging, the fix session), `workspace.py` (copy bundle without `node_modules`/`.venv`/…, `git init`, orchestrator commits, `workspace_tree`); threat model in `SECURITY.md` |
| `exporter.py` | Timestamped Markdown session exports |
| `demo/` | Offline demo mode: `DemoChatModel` + `fixtures.py` (canned, idea-aware output per role) and a stand-in implementation stage and verification lanes |
| `text_utils.py` | `normalize_content()` shared by the LLM layer and the UI service |
| `ui/app.py` | Gradio entrypoint (`make_ui()`, `main()`), started by `idea-to-mvp` / `python -m idea_to_mvp` |
| `ui/service.py` | `SubmitService`: async generator bridge between Gradio and the graph; renders every response from checkpointed state |
| `ui/view.py` | Pure projection of graph state to the UI: `transcript_from_state`, `mode_from_state`, status and stage helpers (no Gradio) |
| `ui/render.py` | HTML helpers for chat cards (`thinking_block`, `turn_block`, ...) |
| `ui/components.py` | Pure HTML components: `stage_stepper` (steps with status and elapsed time), `usage_badge`, `stage_header` (the sticky bar), `model_profile_markdown`, the example ideas |
| `ui/console.py` | Pure renderers for the implementation tab: `render_task_board` (pending / running / done / failed), `render_console` (the live log), `render_cost_meter`, `running_tasks` |
| `ui/dashboard.py` | `render_dashboard`: the delivery dashboard (lane verdict cards, requirements table, cost/time, how to run) |
| `ui/artifacts.py` | The read-only file viewer (`read_workspace_file`, confined to the workspace), the blueprint zip, and the key that tells when the artifacts panel must refresh |
| `ui/gates.py` | `GateSpec` / `GATES`: the form of every gate (widgets, `render`, `build_resume`, `validate`, `echo`, `next_step`), the blueprint editor's `save_blueprint_file`, and the layout helpers that map the flat Gradio inputs/outputs to a gate |
| `ui/theme.py` | `build_theme()` and the `CSS` (all colours are variables, defined for light and dark) |

## State contract

`IdeaDiscussionState` (`state.py`) carries: `user_idea`, `discussion_history` (LangChain messages, `add_messages` reducer), `summary`, `questions` + `generated_questions` (the `MvpQuestion` dumps and their numbered-line rendering), `user_answers`, `architecture_proposal` + `architecture` (the `ArchitectureProposal` dump and its markdown rendering), `arch_choice`, `execution_strategy` (an `ExecutionStrategy` dump), `plan_decision`, `implement_decision` (`{implement, notes, parallel}`: `parallel` is the parallelism picked at the gate, 0 = the setting), `blueprint_docs` (path → generated document, filled in parallel), `blueprint_review` (the critic's latest `{approved, revisions, issues}`), `project_bundle_dir/files/summary`, `workspace_dir`, `implementation_log`, `task_results` (plan task id → `TaskResult`, merged by a dict reducer), `lane_reports` (verification lane → `LaneReport` dump, filled by the parallel lanes through a dict-merge reducer), `finished_tasks` (sessions awaiting their merge in the parallel engine), `verification` (`{passed, attempts, report, lanes}`), `delivery_report`, `delivery_zip`, `iteration` (the version being built: 1 = v0.1), `change_requests` (the feedback of each later iteration), `iterate_decision`, `spent_before_iteration` (cost of earlier versions, so each has its own budget), `stage`, `next_speaker`, `max_rounds` (the panel's turn budget: rounds × speakers), `turn_count`, `panel_mode`, `opening_turns` (speaker → opening statement; filled by parallel branches through a dict-merge reducer), `convergence` (the moderator's latest `{converged, reason}`). Gate decisions are small TypedDicts (`ArchChoice`, `PlanDecision`, `ImplementDecision`).

## The panel

`nodes/panel.py` builds the panel as a LangGraph **subgraph**, mounted in the parent graph as the single node `panel`:

- **Openings in parallel.** The start edge fans out with `Send("opening_turn", ...)`, one per speaker, so the three opening statements cost one call latency instead of three. A speaker's opening sees only the idea (the others are being written at the same time). `merge_openings` then appends them to the transcript in canonical PM, Tech Lead, Skeptic order whichever finished first.
- **Moderated debate.** From then on `moderator → speaker_turn` alternate. The moderator (role `moderator`, a cheap structured `ModeratorDecision` call) picks the next speaker and may declare the debate converged; its `reason` is shown in the chat as the convergence note. Code, not the prompt, enforces the hard rules: nobody converges before every speaker has had two turns, nobody speaks twice in a row (the least-heard speaker is used instead), and `max_rounds` is a hard cap on turns. If the moderator's output stays invalid, the deterministic fallback keeps the panel in rotation.
- **Round robin.** With `panel_mode="round_robin"` the start edge goes straight to `speaker_turn`, which loops in the fixed PM → Tech Lead → Skeptic order until the cap; no moderator, no parallelism.
- **State across the boundary.** The subgraph shares `IdeaDiscussionState` with the parent but declares a narrower input schema (`PanelInput`) that leaves out `usage` and `opening_turns`. Without it the parent's accumulating `usage` channel would receive its own records back from the subgraph and count them twice. The LLM nodes inside the subgraph carry `LLM_RETRY`; the `panel` node itself does not (a retry would redo every turn), and because the subgraph inherits the parent checkpointer, a crash mid-panel resumes from the last finished turn.
- **Concurrency cap.** `graph.run_config(thread_id, max_concurrency=...)` (from `LLM_MAX_CONCURRENCY`) bounds how many model calls parallel branches keep in flight.

## The blueprint pack

`nodes/blueprint_graph.py` builds the pack as a second subgraph, mounted as the parent's `plan_bundle` node. It is an evaluator-optimizer pipeline:

- **Dependency waves.** Every document is generated from the shared context block **plus the upstream documents it `needs`** (`BundleFileSpec.needs`, injected by `blueprints.prompt_for` under `## Upstream document: <path>` headings, each truncated at a line break with a `[truncated]` marker), so the plan is written from the real PRD and architecture and the subagent definitions from the real plan. A wave starts when the previous one is done and its documents are written concurrently: (1) `PRD.md`, `ARCHITECTURE.md`; (2) `README.md`, `AGENTS.md` and the `contracts/`, `application/`, `quality/` guides, and the plan; (3) `.claude/agents/<workstream>.md` (skipped when the strategy has no workstreams). Each `wave_n` node is a no-op that fans out with `Send`, one task per document; the static edge out of the workers fires once the whole wave is done. A worker stores `{path: text}` in `blueprint_docs` (an empty reply becomes the document's fallback right there, so later documents read what will really be written).
- **The plan is typed data.** The `plan_writer` node (role `plan_writer`) produces a `plan.Plan` through `invoke_structured`: contracts, tasks (`T01`…, `depends_on`, `requirement_ids`, contracts in/out, acceptance, tests, coverage, handoff), and the project commands. `plan.validate_plan` then checks it deterministically: duplicate or malformed ids, unknown dependencies, cycles, unknown workstreams or contracts, requirement ids missing from the PRD, P0 requirements no task covers, workstreams with no tasks, an empty test command. Any findings are fed back for **one repair attempt** (kept only if it leaves fewer problems). If the model never produces a valid `Plan`, a deterministic `fallback_plan` is used. `plan.execution_waves` turns the task graph into topological layers for the parallel execution to come. `plan.json` is the source of truth; `plan.md` is rendered from it by `render_plan_markdown`.
- **Critic and revise loop.** After the last wave the `review` node cross-checks PRD ↔ ARCHITECTURE ↔ plan ↔ subagent prompts: the deterministic plan problems that survived the repair become blockers, and a critic model (role `blueprint_critic`, a structured `CritiqueReport`) adds its own issues, each a `blocker` or a `warning` on a named file. A report is approved exactly when it has no blockers, whatever the model claimed. While blockers name a regenerable document and `MAX_BLUEPRINT_REVISIONS` allows, `revise` regenerates **only those documents**, with the issues and their previous version appended to the prompt, and the pack is reviewed again. A stale dependent (subagents of a revised plan) is not regenerated automatically; the next review reports it. When no more revisions are possible, the remaining notes are written to `REVIEW.md` in the bundle, listed in the pack summary, and shown at the implement gate (the plan gate comes before the pack exists, so the implement gate is where the user first approves it).
- **Writing.** The final `write` node calls the deterministic `blueprints.write_bundle()`. As with the panel, the subgraph takes a narrower input schema (no `usage`, `blueprint_docs`, or `blueprint_review`), the retry policy sits on the model nodes, and a crash mid-pack resumes at the failed step without regenerating finished ones.

## Multi-provider LLM abstraction

- `llm.get_runtime(role_key)` returns a cached, frozen `AgentRuntime` (LangChain `BaseChatModel` + provider, model, token budget, system prompt) for any role in `roles.ROLES` (PM = OpenAI, Tech Lead / Summarizer / Architect = Anthropic, Skeptic = Google by default). The provider is always the configured one; `warn_on_provider_mismatch()` only logs an obviously wrong pairing.
- Nodes call `llm.invoke_text(runtime, messages)` and get plain text back ('' when the model produced none). It is the single owner of retries: Google output truncated by `MAX_TOKENS` is retried with a larger `max_output_tokens`, and an empty reply is retried once with an instruction to answer visibly.
- Agents that hand structure to other agents use `llm.invoke_structured(runtime, messages, Schema, fallback=...)` and get a validated pydantic instance. It asks the provider for schema-constrained output (Anthropic: native `json_schema`, because newer Claude models reject forced tool use), retries once with the validation error appended, then uses the deterministic `fallback` (logged) or raises `StructuredOutputError`. Schemas are kept provider-safe: all fields required, and counts/lengths enforced by Python validators rather than JSON-schema keywords. State stores `model_dump()` dicts; prompts no longer describe output formats, the schema does.
- Newer models reject or discourage sampling parameters, so `sampling_kwargs()` only forwards `TEMPERATURE` to models that accept it.

## Demo mode

`DEMO_MODE=true` makes `llm.get_runtime()` return runtimes backed by `demo.models.DemoChatModel`, which answers from `demo/fixtures.py` (`RESPONSES[role]`); blueprint documents are told apart by their system prompt. `implementer.run_implementation` and the lane/fix sessions of `implementation/verify.py` dispatch to `demo/implementer.py`, which writes a tiny dependency-free project and verifies it with real checks (its `unittest` suite, byte-compilation plus a `python -m demo_app` smoke probe, and P0 requirement coverage from `plan.json`). The UI shows a banner. `tests/test_e2e_demo.py` drives the real graph and `SubmitService` through every gate this way, offline, in CI.

**Demo-parity rule:** any new LLM call, structured schema, or SDK agent session needs a fixture in `demo/` so the offline pipeline keeps working end to end.

## Persistence, resume, and the state-derived UI

- **Checkpoints:** `CHECKPOINTER=sqlite` (default) stores every graph checkpoint in `OUTPUT_DIR/sessions.db` through `AsyncSqliteSaver`; `memory` keeps them for the life of the process. Gates are `interrupt()` pauses, so a checkpointer is always required. The aiosqlite worker thread is made a daemon so an open connection can never keep the process alive after Ctrl+C.
- **Async runtime:** the UI drives the graph with `graph.astream(..., stream_mode="updates")`; sync nodes run in worker threads. `GraphProvider` opens the graph lazily inside the event loop that will use it (Gradio builds the UI synchronously but serves from its own loop).
- **The UI is a projection of state.** After every graph update `SubmitService` re-reads the thread's checkpoint (with `subgraphs=True`, and `graph.merged_values` folds the running panel's own checkpoint in, because a subgraph node only commits to the parent when it finishes) and derives the chat (`transcript_from_state`), the UI mode (`mode_from_state`: idea → answers → arch choice → plan gate → implement gate → done, plus `interrupted` for a run that stopped mid-step), the status line, and the stage tracker from it. The browser session only remembers the thread id, so a session can be rebuilt after a restart and no copy of pipeline state can drift. Transient overlays (the message just sent, the step in progress, an error) are added on top of the derived transcript and vanish on the next render.
- **Resume:** the *Saved sessions* panel lists `SessionRegistry` rows; *Resume selected* re-renders that thread at whatever gate it was waiting at. A run that crashed between gates shows a *Continue* button, which calls `astream(None, config)` to carry on from the last checkpoint instead of restarting.

## Resilience, usage, and tracing

- **Two retry layers.** Provider SDKs retry HTTP-level failures themselves (`LLM_MAX_RETRIES`, per-request `LLM_TIMEOUT_SECONDS`). Nodes that call a model (`summarizer`, `architect`, `strategy`, and the panel and blueprint subgraphs' model nodes) also carry `LLM_RETRY`, so a node that still fails with a *transient* error (rate limit, timeout, connection reset, 5xx; matched by exception class name and status code, no provider imports) is re-run with backoff instead of failing the stage. Auth, validation, and schema errors are never retried.
- **Usage.** Model-calling nodes are wrapped with `@with_usage(role=...)`, which records every model call made during the node (text and structured, retries included) via LangChain's usage callback and appends `UsageRecord`s to the accumulating `usage` state channel. `cost_usd` is None for plain model calls (prices are not hard-coded); Agent SDK sessions will report a dollar cost. The UI shows the running token total and agent spend as a badge in the sticky header (`ui/components.usage_badge`).
- **Tracing.** Set `LANGSMITH_TRACING=true` and `LANGSMITH_API_KEY` in `.env` (or the shell). Only `LANGSMITH_*`/`LANGCHAIN_*` are exported from `.env` — provider keys are deliberately not, because agent subprocesses inherit the environment. Runs are named `idea-to-mvp` and tagged `thread:<id>` (`graph.run_config`).
- **`idea-to-mvp doctor [--offline]`** checks every role's key and, unless `--offline`, makes one tiny real call per distinct provider/model; it also checks the bundled Claude Code CLI. Failures are reported per role; exit code 1 if any check failed.

## The implementation engine

For strategy mode `agent_team` with a runnable `plan.json` (tasks exist, ids unique, dependencies known, no cycle; otherwise the lead session is used and a warning is logged), the async `implementer` node calls `executor.run_plan`, which walks `plan.execution_waves` one task at a time (parallel execution is the next step):

- **One fresh session per task.** `run_task` builds a prompt from the `PlanTask` (goal, scope, requirements, acceptance criteria, tests, contracts, the summaries of its dependencies, the project commands), streams the session through `implementer.iter_agent_messages`, and records the `ResultMessage` cost, turns, and session id. On success the **orchestrator** commits `T01: <title>`; a failed session is never committed. A crashing session becomes a `failed` task and the run goes on; tasks whose dependencies did not finish are `skipped`; a missing sandbox is a configuration error and aborts.
- **Resume.** Results are written to `<workspace>/.idea-to-mvp/progress.json` (git-ignored, read-only for agents). Re-running the node after a kill skips `done` tasks and retries `failed` and `skipped` ones in the same workspace.
- **Budgets.** One `BudgetTracker` is shared by all sessions. A session's SDK budget is `min(IMPLEMENTER_MAX_TASK_USD, remaining)`; a task starts only while budget remains; what earlier runs of the workspace already spent is deducted; the verification lanes and fix sessions that follow are paid from what is left of the whole-run cap (see *Verification lanes*).
- **Live events.** The node publishes `ImplEvent`s (`task_start`, `tool`, `text`, `cost`, `task_end`) on LangGraph's custom stream; `SubmitService` subscribes with `stream_mode=["updates", "custom"]` and shows progress and the running cost (`ui/view.implementation_progress`, throttled; transient, not part of the checkpoint). The lead session mode emits no events. Each task's cost is also appended to `usage` as an `implementer` record.
- **Demo mode** simulates each task (`demo_task`): the first builds the stand-in project, every task writes a note, with the same events and commits.

### Parallel execution

With `IMPLEMENTER_MAX_PARALLEL` above 1 (default 3), strategy mode `agent_team`, a runnable plan, and a git workspace, `route_after_workspace` sends the run to the `implement_plan` subgraph (`nodes/implement_graph.py`) instead of the single-node `implementer`; anything else keeps the sequential engine above, which is exactly the `=1` behaviour.

- **Loop.** `prepare` discards worktrees of a killed run; `pick_wave` skips tasks behind a failed one, takes the ready tasks (dependencies *merged*, `scheduler.next_ready`) and creates one worktree per task; `run_task_node` runs the sessions concurrently; `merge_wave` merges; back to `pick_wave` until nothing can start; `finish` writes the log and usage, commits, and removes leftovers.
- **One worktree per task.** `merge.create_worktree` makes `<projects>/.worktrees/<workspace>/<id>` on branch `task/<id>` from the current HEAD, beside the workspace (not inside it). That folder is the session's `cwd`, sandbox root, and guard root, so a task cannot touch the main checkout or another task. The task prompt says dependencies must be installed there.
- **Bounded parallelism.** A node cannot set its own concurrency, so a batch never holds more than `IMPLEMENTER_MAX_PARALLEL` tasks (`max_concurrency`/`LLM_MAX_CONCURRENCY` still caps the run overall).
- **Budget by reservation.** Every started task reserves `min(IMPLEMENTER_MAX_TASK_USD, whole-run budget)`; a batch holds only as many tasks as `floor(remaining / reservation)`, so concurrent tasks can never overshoot the whole-run cap; when less than one task's reservation is left, the ready tasks fail as "not started: budget" and their dependents are skipped. A resolver is paid from what is left.
- **Merge, then done.** The orchestrator commits the task on its branch (`--git-dir` pinned to the worktree's admin directory inside the main repository, because an agent can rewrite its worktree's `.git` file) and `merge_wave` merges branches in task-id order (`T03: <title> (merged)`; fast-forward when main has not moved). A task is recorded `done` in `progress.json` only after its merge, so a kill between session and merge loses nothing recorded; on resume the stale worktree is discarded and the task redone.
- **Conflicts.** A conflict leaves the merge in progress and `executor.run_merge_resolver` runs one bounded session in the main workspace (keep both intents, run the tests, no commit). The orchestrator accepts the result only if no conflict markers remain (`merge.complete_merge`); otherwise the merge is aborted, the task fails, and its dependents are skipped.
- **Project tests are not run on the host.** Running `plan.commands.test` from the orchestrator would execute agent-written code outside the sandbox. Each task's own session runs its tests, the resolver runs them after a conflict, and the verification stage checks the integrated result.
- **Live view.** `ui/view.implementation_progress` shows every running task; a task's end is announced by the merge step with its final status. The implement gate reports the plan's `task_count`, `dag_width`, and `max_parallel`.

## Verification lanes

```
implement → verify ─Send×3→ verify_lane → verdict ─ pass | exhausted ─→ delivery_report
               ↑                              └────── retry ──→ fix ──┘ (back to verify)
```

- **Three lanes, concurrently.** `verify` fans out with `Send` to `verify_lane`, one branch per lane (`implementation/verify.py`): **tests** installs dependencies and runs `plan.commands.test` (it may write coverage artefacts), **quality** runs lint / type checks, looks for obvious security problems, and smoke-probes the documented run command (skipped, with a note, for a library), **requirements** maps every P0 requirement of the PRD (all of them if none is marked P0) to at least one test and reports the uncovered ones. Quality and requirements sessions run with `Edit`/`MultiEdit`/`Write` disallowed; a lane can still create files through the shell, so treat them as read-mostly. The quality lane must not run the install command (the tests lane does, at the same time).
- **Structured verdicts, no sentinel.** Each session is asked for JSON matching `LaneReport` (`ClaudeAgentOptions.output_format`, `ResultMessage.structured_output`; JSON in the reply text is accepted as a fallback). A missing, non-JSON, schema-invalid, wrong-lane, or error result fails *that lane with the reason* (`parse_lane_report` never raises); a lane that lists failures does not pass. `passed` overall needs all three lanes.
- **Graph-level fix loop.** `verdict` folds the lanes into `state["verification"]` (`{passed, attempts, report, lanes}`) and routes `pass` / `exhausted` (attempts reached `MAX_FIX_ATTEMPTS`, or no budget for another round) to `delivery_report` and `retry` to `fix`. `fix` runs one implementation session on the *merged failure list* (`[lane] failure`, not raw prose), the orchestrator commits (`fix attempt N`), and control returns to `verify`. Every attempt is its own checkpoint and stream event, so the UI shows the live lane progress (`verify:tests`, ... in the console) and a killed run resumes at the step it stopped at. A retry re-runs all three lanes (their reports overwrite the previous round's).
- **Budget.** Lane and fix spend is recorded in `usage` (`verifier`, `fixer`). Each lane may spend `min(IMPLEMENTER_MAX_TASK_USD, remaining / 3)`, a fix session `min(IMPLEMENTER_MAX_TASK_USD, remaining)`, where `remaining` is the whole-run cap minus everything in `usage`. With nothing left `verify` skips the round and the report says so.
- **Delivery report.** Shows a verdict per lane and the failures each reported.

## Iteration loop

```
delivery_report ─(iteration < MAX_ITERATIONS)→ iterate_gate ─ iterate ─→ change_planner → implement → verify → delivery_report
                └────────────── otherwise ───→ END              └─ done ─→ END
```

- **Gate.** `iterate_gate` interrupts with `{"kind": "iterate_gate", "report", "iteration", ...}` and resumes with `{"iterate": bool, "feedback": str}`; iterating without feedback ends the run. On iterate it appends the feedback to `change_requests` and moves `iteration` on (v0.1 → v0.2). After `MAX_ITERATIONS` versions (default 5) the gate is not offered.
- **`change_planner`** (role `change_planner`) receives the feedback, the PRD, the existing tasks with their status, the contracts, and the id prefix, and returns a `ChangePlan` of *new* tasks (`I2-01`, ... for iteration 2; they may depend on finished tasks). The result is checked against the extended plan with `validate_plan` (problems the plan already had are not held against the change, ids must be `I<n>-01`, `I<n>-02`, ... and never reuse an existing id); one repair attempt gets the problem list, and a still-invalid plan becomes one deterministic task carrying the request (`fallback_change_tasks`). The tasks are appended to the workspace's `plan.json`/`plan.md` (finished tasks are never rewritten) and committed (`I2: plan`), because task worktrees are made from HEAD.
- **Same engine, only new tasks.** `route_after_workspace` sends the run to `implementer` or `implement_plan` exactly as for the first version, and an iteration always builds task by task (`builds_task_by_task`), even when the strategy is `subagents`; tasks `done` in `progress.json` are skipped, so only the new ones run. A lead-session project has no per-task progress, so the planner records its tasks as done first. Verification then starts from scratch (`verification` is reset).
- **Budget per version.** The whole-run cap applies to each version: the engines count only the spend of the current version's tasks (`progress.spent_in_iteration`), and verification subtracts `spent_before_iteration` from the run's `usage`, so a full first build does not starve the second.
- **Delivery.** `delivery_report` tags the workspace `v0.<iteration>` when verification passed (uncommitted work is committed first, an existing tag moves) and writes `deliveries/<workspace>-v0.<iteration>.zip` under `OUTPUT_DIR`, referenced as `delivery_zip`. The zip leaves out dependency and build directories, `.git`, orchestrator files, `.env*` (except `.env.example`), and symbolic links. In the UI the gate is its own mode: a *Request changes* / *Finish here* choice plus the feedback box.

## Implementation sandbox

Every agent session is built by `implementation.options.build_agent_options`, which combines an OS sandbox for shell commands (`IMPLEMENTER_SANDBOX`, domains in `IMPLEMENTER_ALLOWED_DOMAINS`), the `guard.py` `PreToolUse` hook bound to the session's workspace (the only path check for file tools), a scrubbed environment (the SDK merges `env` over the inherited one, so secrets are blanked with empty strings), and `setting_sources=[]` so neither user nor workspace settings load. `sandbox_status(settings)` is what the implement gate reports, so the UI never claims more than is true. Agents never commit: `workspace.commit_workspace` does, with hardened git. What each layer does and does not protect is in `SECURITY.md`, its single owner.

## Streaming UI

`SubmitService.handle_submit()` is an async generator that yields one Gradio update tuple per graph update (panel-internal updates included, via `astream(..., subgraphs=True)`), so the chat streams as nodes and panel turns finish. Gate decisions become `Command(resume=...)` payloads; interrupts select the next UI mode.

- **Implementation tab.** The UI has two tabs: *Conversation* (chat, idea box, gate forms) and *Implementation*: a cost meter (agent spend of the current version against `IMPLEMENTER_MAX_TOTAL_USD`), the **task board** (`plan.json` tasks in pending / running / done / failed columns with workstream, branch, cost, turns, commit, and an expandable summary plus `git diff --stat` of the task's commit; skipped tasks count as failed), the **live console** (the `ImplEvent`s of the custom stream, latest 200, oldest first, scroll anchored to the bottom, verification lanes included as `verify:<lane>`) and the artifacts panel. Events are transient by nature: `SubmitService` keeps them per thread in memory (`_events`), so the console is empty after a restart, while the board is derived from what is on disk (`plan.json`, `progress.json`, the state's `task_results`), and a task counts as *running* only while a run is streaming (`_active`), so a stopped run never leaves tasks marked running. Diff stats are cached per commit (`workspace.diff_stat`, hardened git, first parent so a merge shows what it merged). The service selects the implementation tab when a run starts building and the conversation tab when it ends, only when that changes (it reads the state's own `stage`: the view's derived stage flickers to `done` between the steps of a subgraph).
- **Artifacts panel.** Workspace path with a copy button, download buttons for the delivery zip and the blueprint folder zip (both served from `OUTPUT_DIR/deliveries`, the only `allowed_paths` of the app), and a read-only file explorer. A `gr.FileExplorer` cannot change its root, so it is re-created by `@gr.render` whenever the workspace-path `State` changes; its text is shown through `SubmitService.view_file` → `read_workspace_file`, which refuses anything outside the workspace (symbolic links that lead out are not followed), `.env*` files (except `.env.example`), `.git`, large (>200 KB) and binary files. The panel is refreshed only when what it shows changes (`artifact_key`: workspace, delivery zip, newest blueprint file).
- **Delivery dashboard.** The chat's delivery entry is `render_dashboard(state)`, not markdown: a verdict card per verification lane (commands, failures), a requirements table (each PRD requirement with its priority, covering tasks, and a status: delivered / incomplete / untested when the requirements lane flagged it / uncovered), agent cost, tokens, total time (from the stage timings), tasks done, and *How to run* (the plan's install and run commands, else the README's run section). It replaces the separate verification entry once the run is delivered; the iterate form sits right below it. The markdown report stays in state (`delivery_report`) for the iterate gate and the session export.
- **Gate forms.** Every gate has its own form (`ui/gates.py`); none reuses another's widgets, and the old shared textbox/radio (and `DECISION_CHOICES`/`_INPUT_LABELS`/`_BUTTON_LABELS`) are gone. A `GateSpec` holds: `build()` (creates the widgets), `render(payload)` (fills them from the interrupt payload as `gr.update` kwargs), `build_resume(inputs)` (form inputs → exactly the value the node's payload parser takes; tests round-trip each through its node), `validate(inputs, payload)` (refuses a bad submission with a message), `echo(inputs)` (the decision as a chat message until the graph records it), `next_step(resume)` (the node that runs next, for the progress indicator) and an optional `wire()` for the form's own events. `SubmitService` looks the pending gate up in `GATES`; the app lists every gate widget in `gate_layout()` order and each submit button injects its own action name (`pack_inputs`). Outputs are the shared widgets followed by one visibility update per gate panel and one update per gate widget (only the pending gate's are filled; all forms hide while a decision is being sent or a step runs).
  - **answers**: five fields prefilled with each question's `suggested_answer` and labelled with `why_it_matters`, a *Use all suggestions* button; the resume is the numbered `1. …` lines (blank answers keep their number, trailing blanks drop; an all-blank form is refused).
  - **arch_choice**: two side-by-side cards (name, style, stack chips, data, trade-offs, limits) with a *Recommended* badge and the rationale, a radio preselected to the recommendation, and notes.
  - **plan_gate**: notes plus *Generate the execution pack* / *Skip for now*.
  - **implement_gate**: a summary table (model, sandbox, permission mode, tasks, DAG width, budget, estimated sessions), a red banner when the sandbox is off or the mode is `bypassPermissions`, the review notes, a **parallelism selector** (`Sequential | Parallel N` up to the plan's DAG width; sent as `parallel` and honoured by the routing and the parallel engine through `effective_parallel`), and the **blueprint editor**: `PRD.md`, `ARCHITECTURE.md`, `plan.md` in a code editor, saved into the pack before anything is built. A save is validated: empty files are refused; a `PRD.md` edit is checked against the current plan; a `plan.md` edit is parsed (`parse_plan_markdown`) and checked with `validate_plan` (cycles, unknown ids, ...), and on success `plan.json` (the source of truth) and a canonical `plan.md` are written. Only problems the edit introduces block it. *Start implementation* is refused while the editor holds unsaved changes.
  - **iterate_gate**: a feedback box with *Iterate* / *Finish*.
- **Adding a gate** is one node (which `interrupt()`s with a payload) plus one `GateSpec` in `GATES`; the layout, the service dispatch, and the stop wiring follow from the registry.
- **Design system.** All colours live in CSS variables (`ui/theme.py`: `:root` for light, `.dark` for Gradio's dark scheme); rules use only `var()` and `color-mix()`, so cards are tinted by their accent in both schemes. `tests/test_ui_render.py` fails on a hard-coded hex colour outside the variable blocks and checks text contrast in both schemes. The theme and CSS are passed to `launch(theme=, css=)` (Gradio 6).
- **Sticky header.** `stage_header` = the stage stepper (done / active / todo / failed) with the time each stage took, plus the token/$ badge. Elapsed time is *derived*, not stored: `SubmitService` reads the thread's checkpoint history and `view.stage_elapsed` credits each checkpoint with the time since the previous one, skipping time spent waiting at a gate (`GATE_NODES`); the running step gets the time since the last checkpoint. The marks are re-read only after the graph committed a step.
- **Settings.** The header accordion holds rounds, panel mode, autopilot, and a read-only model profile (from `Settings`); `handle_submit(text, rounds, decision, thread, panel_mode, autopilot)` receives them. The chat is `70vh` tall and example ideas replace the old pre-filled text (they are hidden while a gate is open).
- **Autopilot.** `state["autopilot"]` (set from the checkbox; sent with every resume as `Command(update={"autopilot": ...})`, or `aupdate_state` when continuing) makes `collect_answers`, `arch_choice` and `plan_gate` decide without pausing: each question's `suggested_answer` numbered like a typed answer (the gate still asks if any suggestion is missing), the architect's `recommendation`, and generate. The implement gate never checks it (it spends money) and the iterate gate keeps asking; the decisions carry an "Autopilot" note in the chat.
- **Stop.** The Stop button is wired with `cancels=[run_event]`: Gradio cancels the running `handle_submit`, which cancels `astream` between checkpoints, and `SubmitService.stop_run` re-renders from the last checkpoint. State stays consistent (a step that was running writes nothing); the run shows as `interrupted` and *Continue* redoes that step. A synchronous node already running in a worker thread finishes in the background and its result is discarded.

## Blueprint pack

When the user accepts the plan gate, the pack (generated as described above) is written to a timestamped folder in `blueprints_dir`: `README.md`, `PRD.md` (numbered requirements), `ARCHITECTURE.md`, `AGENTS.md` plus scoped `contracts/`, `application/`, `quality/` guides, `plan.json` (the validated task graph, source of truth) with `plan.md` (rendered from it), `.claude/agents/*.md` (one subagent per workstream), `STRATEGY.json`, and `REVIEW.md` when review notes remain. Each document has its own system prompt, wave, and upstream needs in `blueprints.py`.

## Output locations

All generated data goes under `OUTPUT_DIR` (default `~/idea-to-mvp`, outside the repository): `exports/`, `blueprints/`, `projects/`, `deliveries/` (one zip per delivered version), and `sessions.db` (checkpoints + session list).

## Conventions

- **Imports:** absolute (`from idea_to_mvp.config import get_settings`); no relative or dual-import fallbacks. The package is installed editable by `uv sync`, so tests and the app import it the same way.
- **Calling the LLM layer:** nodes use `from idea_to_mvp import llm` and call `llm.get_runtime(...)` / `llm.invoke_text(...)` / `llm.invoke_structured(...)` through the package namespace, and use `from idea_to_mvp import implementer` for SDK sessions, so a test can monkeypatch one attribute for every node.
- **Test isolation:** `config.clear_settings_cache()` resets the `lru_cache` between tests; point `OUTPUT_DIR` at `tmp_path` via `monkeypatch.setenv`. Graph lifecycle tests fake all LLM/SDK calls by monkeypatching `llm.get_runtime`, `llm.invoke_text`, `llm.invoke_structured` (answer from `demo.fixtures.respond_structured`), and `implementer.prepare_workspace` / `run_implementation` and `implementation.verify.run_lane` / `run_fix` — see `tests/test_graph_lifecycle.py`.
- **Driving the service in tests:** use the `demo_service` fixture (`tests/conftest.py`: demo models + a real SQLite checkpointer under `tmp_path`) and `tests/service_helpers.py`; graph-level tests use `build_graph(MemorySaver())`.
- **Model-calling nodes:** decorate with `@with_usage(role=...)` and register them with `retry_policy=LLM_RETRY` (in `graph.py`, or in the subgraph builder for the panel and blueprint nodes).
- **Agent sessions:** build options only through `implementation.options.build_agent_options`; never construct `ClaudeAgentOptions` elsewhere, or the sandbox, guard, and environment scrub are skipped.
- **Demo parity:** new LLM calls and agent sessions get a demo fixture (see Demo mode); `tests/test_e2e_demo.py` fails otherwise.
- **Graph diagram:** after changing graph topology run `uv run python scripts/gen_graph_diagram.py`.

# Idea-to-MVP Orchestrator Overhaul — Design

**Date:** 2026-06-11
**Status:** Approved (roadmap + Phase 1)
**Scope of this document:** The end-state roadmap for the full overhaul, and the detailed design for Phase 1 (orchestrator core rebuild). Phases 2–4 are scoped here at the roadmap level and will each get their own design document before implementation.

## Goals

Turn `idea-to-mvp` from a discussion-and-blueprint generator into a complete idea-to-first-product orchestrator:

- Every pipeline stage is performed by a distinct, clearly defined agent.
- The pipeline ends with real, tested code for the user's MVP — not just instruction files.
- Human-in-the-loop gates protect the user at every cost- or direction-changing transition.
- The codebase is a high-quality open-source LangGraph reference: clean graph design, data-driven roles, proper interrupt-based HITL, tests, and documentation.

## Decisions already made

| Decision | Choice |
|---|---|
| Implementation-stage engine | Claude Agent SDK (Python `claude-agent-sdk`), driven from a LangGraph node |
| UI stack | Stay on Gradio; redesign the UX within it |
| Autonomy model | Gated stages — explicit user approval at each major transition, with a cost warning before implementation |
| Delivery sequencing | Four phased sub-projects, each landing in a working, tested state |

## End-state pipeline (target after all phases)

```
Idea
 → Discussion loop        (PM / Tech Lead / Skeptic, round-aware, converging)
 → Summarizer             (executive brief + exactly 5 MVP questions)
 🔒 GATE: user answers the questions
 → Architect              (2 options + recommendation, anchored to user constraints)
 🔒 GATE: user picks an architecture option
 → Strategy agent         (auto-selects execution mode: Subagents vs Agent Team)
 → Planner                (Blueprint v2: PRD, architecture doc, contracts, plan,
                           .claude/agents/*.md, execution strategy manifest)
 🔒 GATE: user approves blueprint and explicitly starts implementation (cost warning)
 → Implementation stage   (Claude Agent SDK session(s) build the project in
                           generated_projects/<slug>/)
 → Verifier               (runs the generated project's test suite; bounded fix loop)
 → Delivery report        (file tree, test results, how to run the product)
```

## Phase roadmap

### Phase 1 — Orchestrator core rebuild (designed in this document)
Interrupt-based HITL, single-thread sessions, role registry, prompt overhaul, simplified `SubmitService`, extended state, rewritten tests.

### Phase 2 — Blueprint v2 + strategy agent
- Much richer generated pack: `PRD.md`, `ARCHITECTURE.md`, contract files, a detailed `plan.md` with acceptance criteria per task, and a generated-project `README` scaffold.
- Generated `.claude/agents/*.md` subagent definitions tailored to the project's workstreams.
- A strategy node that classifies the project (number of parallelizable workstreams, coupling between tasks, expected size) and emits an execution strategy manifest choosing **Subagents** (single lead session with specialized subagents) or **Agent Team** (multiple coordinated sessions), plus the reasoning.

### Phase 3 — Implementation + testing stage (Claude Agent SDK)
- New graph nodes: `implementer` and `verifier`, plus a `delivery_report` node.
- Workspace: `generated_projects/<timestamp>-<slug>/`; blueprint files are copied in; SDK agents work only inside this directory (sandboxed `cwd`, allowed-tools restricted to it).
- `implementer` runs the execution strategy from Phase 2 via `ClaudeSDKClient`, streaming progress events (current task, files written) into the UI.
- `verifier` runs the generated test suite inside the workspace; on failure, a bounded fix loop (configurable, default 2 attempts) feeds failures back to the implementer; final status is reported honestly either way.
- Cost controls: explicit start gate with a model/size-based estimate, per-run token budget setting, hard stop when exceeded.

### Phase 4 — UX redesign + open-source documentation
- Gradio redesign: pipeline stage tracker, per-agent styled cards/avatars, collapsible stage sections, a structured form for answering the 5 questions, architecture option picker, implementation progress view with file tree, delivery report screen.
- Docs: rewritten `README.md` (with pipeline diagram), updated `CLAUDE.md`, `CONTRIBUTING.md`, `CHANGELOG.md`, `.env.example`, and a `docs/` architecture page.

## Phase 1 detailed design

### Problem

Human-in-the-loop is currently simulated outside the graph. `SubmitService` rebuilds a fresh `IdeaDiscussionState` from UI strings on every user turn and re-enters the graph routed by a `phase` flag; the plan-bundle step even runs on a second checkpointer thread (`{thread_id}-plan`). Consequences:

- The checkpointer never holds one continuous session, so `route_from_start` exists purely to compensate for lost continuity.
- Session data (idea, summary, questions, answers, architecture) is smuggled through Gradio state lists and recovered by scanning chat transcripts (`_latest_transcript_content`, first-user-message scans).
- The plan gate is parsed from free text with `_is_affirmative`/`_is_negative` prefix matching, which is fragile and locale-hostile.

### 1. Graph: one linear flow with interrupts

`graph.py` becomes a single linear graph; one thread per session, checkpointer always on for interactive use:

```
START → discussion ⟲ (until turn_count ≥ max_rounds)
      → summarizer
      → collect_answers      [interrupt: returns questions; resumes with answers text]
      → architect
      → plan_gate            [interrupt: returns offer + architecture; resumes with
                              {"generate": bool, "notes": str}]
      → plan_bundle | END    (conditional on the gate decision)
      → END
```

- `collect_answers` and `plan_gate` are thin nodes that call `langgraph.types.interrupt(payload)` and write the resume value into state. Interrupt payloads carry everything the UI needs to render the gate (questions list, offer question, architecture text).
- `route_from_start` and the `phase` field are deleted. `route_after_discussion` stays (loop control).
- The plan gate resume value is structured (`{"generate": bool, "notes": str}`) — the UI sends it from a button/checkbox decision. `_is_affirmative`/`_is_negative` are deleted. A "no" at the gate routes to END; the thread remains resumable only insofar as the user starts a fresh run (re-offering later is out of scope for Phase 1).
- `build_graph` keeps the lru_cache and `enable_checkpointer` escape hatch for tests.

### 2. Role registry (`roles.py`, new module)

A single declarative registry replaces prompt/model wiring scattered across `agents.py` and `config.py` accessors:

```python
@dataclass(frozen=True)
class RoleSpec:
    key: str                 # "pm", "tech_lead", "skeptic", "summarizer", "architect", "planner"
    display_name: str
    provider_setting: str    # name of the Settings field, e.g. "pm_provider"
    model_setting: str
    max_tokens_setting: str
    system_prompt: str

ROLES: dict[str, RoleSpec] = {...}
```

- `agents.py` keeps node functions and LLM invocation (`_build_llm`, `_invoke_with_runtime`, fallback/retry logic) but builds runtimes generically from `ROLES` — one cached `get_runtime(role_key)` instead of three near-identical cached getters.
- Speaker order, name tokens, and display names derive from the registry.
- Phases 2–3 add roles (strategy, implementer, verifier) as new registry entries.

### 3. Prompt overhaul

Same output contracts, higher signal. Concrete changes:

- **Round awareness:** every discussion turn prompt states "This is round *k* of *n* for you." Final-round instruction: converge — state your position on the open disagreements and what you'd commit to; do not open new threads.
- **Skeptic becomes constructive:** every objection must be paired with the cheapest test or design change that would resolve it. Prevents pure negativity loops.
- **PM and Tech Lead de-duplication:** explicit instruction to *not restate points already made; reference them by speaker and build or disagree.*
- **Summarizer:** must capture *disagreements and how/whether they were resolved* as a dedicated section, since that is the highest-value content for the architect; dedupe instruction retained.
- **Question generator:** questions must be ones whose answers *change* the architecture or scope (decision leverage test stated in the prompt).
- **Architect:** options must explicitly reference the user's answers ("anchor each option to at least two stated constraints from the user's answers"); recommendation must name the tradeoff being accepted.
- Blueprint prompts (`ROOT_AGENTS_SYSTEM` etc.) are untouched in Phase 1 — they are Phase 2 scope.

### 4. `SubmitService` simplification

- One stream-runner: `run(thread_id, input_or_resume)` that calls `graph.stream(...)` with either initial state or `Command(resume=...)`, translating node events and `__interrupt__` events into UI updates.
- The UI mode is derived from which interrupt the graph is paused at (reported in the stream), not from UI-side mode flags. `mode_state` collapses to a mirror of the graph's pause point.
- Transcript/chat bookkeeping stays (needed for rendering and export) but stops being the source of truth for pipeline data — state lives in the checkpointer.
- Error handling: per-stage try/except retained; `_error_hint` retained; a failed run leaves the thread resumable where possible and tells the user which stage failed.

### 5. State extension (`state.py`)

```python
class IdeaDiscussionState(TypedDict):
    user_idea: str
    discussion_history: Annotated[list[BaseMessage], add_messages]
    summary: str
    generated_questions: list[str]
    user_answers: str
    architecture: str
    plan_decision: PlanDecision     # new: structured gate result {generate, notes}
    project_bundle_dir: str
    project_bundle_files: list[str]
    project_bundle_summary: str
    next_speaker: str
    max_rounds: int
    turn_count: int
    stage: Stage                    # new: enum of pipeline stages, written by each node
```

- `phase`, `plan_offer_question`, `planning_request` are removed/absorbed (`plan_offer_question` becomes part of the `plan_gate` interrupt payload; `planning_request` becomes `plan_decision.notes`).
- `stage` is informational (drives the Phase 4 stage tracker; useful for export now).
- Reserved-but-unused fields are *not* added for Phases 2–3 (YAGNI); the TypedDict is additive later. In particular, the user's architecture-option choice gate (and its state field) arrives with Phase 2, when the strategy agent and planner consume it; in Phase 1 a user can express a preference via the plan gate's `notes`.

### 6. Testing

- `test_agents_routing.py` → rewritten around the new graph: with a fake-LLM runtime, assert the full interrupt/resume lifecycle — run to first interrupt, resume with answers, run to plan gate, resume with `{"generate": False}` ends; `{"generate": True}` produces the bundle.
- `test_submit_service.py` → rewritten for the stream-runner: mode transitions mirror graph pause points; error paths per stage.
- New `test_roles.py`: registry completeness (every role has prompt/model/provider settings that exist on `Settings`), speaker order derivation.
- Existing fake-LLM/test-isolation patterns (`clear_settings_cache`, `clear_runtime_caches`, `clear_graph_cache`) are kept.
- `pytest` and `ruff check .` green is the phase exit criterion; CI workflow unchanged.

### Backward compatibility & migration notes

- Old exports and existing `project_blueprints/` outputs are unaffected.
- The Gradio component layout changes minimally in Phase 1 (the plan gate becomes a structured control instead of free-text yes/no); the full visual redesign is Phase 4.
- `MemorySaver` remains the checkpointer (sessions are per-process); durable persistence is out of scope for this overhaul.

### Error handling summary

- LLM invocation retries/fallbacks (`OPENAI_FALLBACK_MODEL`, Gemini MAX_TOKENS retry) are preserved unchanged.
- A stage failure surfaces in chat with the stage name and hint, and the graph thread stays at its last checkpoint so a retry resumes rather than restarts where the failure was at an interrupt boundary.

## Out of scope (explicitly)

- Switching off Gradio; durable (DB-backed) checkpointing; multi-user deployments; re-offering the plan gate after a "no" within the same thread; provider expansion beyond OpenAI/Anthropic/Google; streaming token-level output (node-level streaming retained).

## Phase exit criteria (Phase 1)

1. One graph thread carries a session from idea to bundle with two real interrupts.
2. `phase`-flag routing, second checkpointer thread, and yes/no text parsing are gone.
3. Roles are declared in one registry module.
4. Updated prompts in place; discussion output is round-aware and converging.
5. All tests pass; lint clean; README "Workflow" section corrected if behavior visibly changed.

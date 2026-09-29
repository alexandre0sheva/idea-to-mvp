# idea-to-mvp Improvement Plan (v0.3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. The owner will ask for tasks one at a time ("implement task N"). Do exactly that task, nothing from neighbouring tasks. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Turn idea-to-mvp from a linear, in-memory, sequential demo into a durable, parallel, sandboxed, observable multi-agent pipeline with a polished UI — a portfolio-grade showcase of LangGraph orchestration that also really produces working first versions of products.

**Architecture:** Keep the single top-level LangGraph thread with `interrupt()` gates. Introduce typed (Pydantic) outputs between agents, subgraphs for the panel / blueprint / implementation phases, `Send` fan-out for parallelism (panel openings, blueprint documents, plan tasks in git worktrees, verification lanes), a durable SQLite checkpointer, and a UI that is a pure projection of graph state.

**Tech Stack:** Python 3.11+, uv, LangGraph 1.x, LangChain 1.x (OpenAI / Anthropic / Google), Claude Agent SDK, Gradio 6, Pydantic 2, pytest (+pytest-asyncio), ruff, mypy.

## How to use this plan

- One task per request. Each task ends in a green suite and updated docs. **Never commit or push** — leave changes in the working tree; at the end, report a suggested commit message.
- Model hint per task: **Sonnet** = mechanical / tightly specified, **Opus** = design-heavy or cross-cutting.
- Tasks are ordered by dependency. Phase 3 (UX) tasks may be pulled forward after their listed dependencies if you want visible wins earlier.
- Paths from Task 2 onward use the new layout (below). Tasks 1–2 use current paths.

## Global Constraints (apply to every task)

1. **Definition of done:** `uv run ruff check .` clean, `uv run pytest` green, `uv run mypy src` clean (from Task 1), demo mode e2e green (from Task 4), docs updated per the ownership map, one line per user-visible change under `## Unreleased` in `CHANGELOG.md`.
2. **Docs ownership — never duplicate knowledge, update the owner only:**

   | Knowledge | Single owner |
   |---|---|
   | What the project is, 60-second quick start, screenshots | `README.md` |
   | Every environment variable, default, and meaning | `.env.example` (commented; README links to it, never repeats the table) |
   | Graph topology (auto-generated mermaid), state contract, LangGraph patterns used, module map, extension guide | `docs/architecture.md` |
   | Agent-execution threat model, sandbox behaviour, reporting | `SECURITY.md` |
   | Dev setup, workflow, quality commands | `CONTRIBUTING.md` |
   | AI-assistant conventions only (commands, import/test conventions) | `CLAUDE.md` (short; links to `docs/architecture.md`, no architecture copy) |
   | User-visible changes | `CHANGELOG.md` |

3. **Demo-parity rule (from Task 4):** any new LLM call, structured schema, or SDK agent session must get a demo-mode fixture so `DEMO_MODE=true` keeps running the whole pipeline offline.
4. **Graph-diagram rule (from Task 3):** any task that changes graph topology must regenerate `docs/architecture.md`'s mermaid block (`uv run python scripts/gen_graph_diagram.py`); a test enforces sync.
5. **Config rule:** new settings go in `Settings` (typed, with validation) and `.env.example` in the same task.
6. **Tests:** TDD — write the failing test first, run it, implement, run again. LLM/SDK calls are always faked in unit tests; real-API tests carry `@pytest.mark.live` and are excluded by default.
7. **No behaviour drift in refactor tasks (2, 3):** the existing tests must pass unchanged except for imports.

## Target layout (after Tasks 2–3; later tasks add to it)

```
pyproject.toml            deps + tooling (single source; requirements.txt removed)
uv.lock
src/idea_to_mvp/
  __init__.py  __main__.py         `idea-to-mvp` console script / python -m idea_to_mvp
  config.py                        Settings (+ output paths)
  state.py                         graph state + make_initial_state()
  graph.py                         build_graph()
  roles.py                         prompts + role registry
  llm/      runtime.py providers.py structured.py     model factory, invoke helpers
  nodes/    discussion.py summary.py architecture.py strategy.py blueprint.py
            implement.py verify.py report.py gates.py
  blueprints.py                    document specs + bundle writing (LLM-free)
  implementer.py                   Agent SDK sessions (split into implementation/ in Task 11)
  exporter.py  text_utils.py
  ui/       app.py service.py render.py
tests/  scripts/  docs/
```

---

# Phase 0 — Foundation

### Task 1: Toolchain, dependencies, model IDs

**Model:** Sonnet · **Depends on:** — 

**Why:** The repo `.venv` is broken (`Bad CPU type in executable` — x86 interpreter on arm64), `requirements.txt` is unpinned with no lockfile, `pyproject.toml` duplicates dependency knowledge, CI installs ad hoc, and `.env.example` model IDs (`claude-4-6-sonnet-latest`, `claude-opus-4-8`, `gpt-5.4-mini`, `gemini-3-flash-preview`) were never validated. Baseline verified on latest packages: 42 tests pass, ruff clean (gradio 6.29, langgraph 1.2, langchain-core 1.6, claude-agent-sdk 0.2.162).

**Files:**
- Modify: `pyproject.toml`, `.github/workflows/ci.yml`, `.env.example`, `config.py`, `app.py`, `CONTRIBUTING.md`, `CLAUDE.md`, `README.md` (Install/Development sections), `CHANGELOG.md`, `.gitignore`
- Delete: `requirements.txt`, `.venv/` (broken; recreated by uv)
- Create: `uv.lock` (generated)

**Interfaces:** Produces the commands every later task uses: `uv sync`, `uv run pytest`, `uv run ruff check .`, `uv run mypy src`.

- [ ] **Step 1:** Delete `.venv`, run `uv sync` after step 2, confirm `uv run pytest` reproduces 42 passing.
- [ ] **Step 2:** In `pyproject.toml`: move runtime deps into `[project].dependencies` with floors set to today's versions (`gradio>=6.29`, `langgraph>=1.2`, `langchain-core>=1.6`, `langchain-openai>=1.6`, `langchain-anthropic>=1.7`, `langchain-google-genai>=4.4`, `pydantic-settings>=2.15`, `claude-agent-sdk>=0.2.162`, `markdown>=3.7`, `python-dotenv`); add `[dependency-groups] dev = [pytest, pytest-asyncio, pytest-cov, ruff, mypy]`; set `asyncio_mode = "auto"`; register marker `live`; bump `version = "0.3.0.dev0"`. Remove the `[project.optional-dependencies].dev` block.
- [ ] **Step 3:** Remove Gradio-version shims in `app.py` (`_supports_param`, the `type=` Chatbot branch): Gradio 6 takes `css`/`theme` in `launch()`, not `Blocks()`, and `Chatbot` is always messages-format. Keep `make_ui()` returning `gr.Blocks` and add a module constant so Task 16 can pass `theme`/`css` to `launch()`.
- [ ] **Step 4:** Validate model IDs: use the `claude-api` skill for Anthropic IDs (expected: `claude-sonnet-5-5` for summarizer/architect, `claude-haiku-4-5-20251001` for Tech Lead, `claude-opus-5-5` for implementer) and context7 (`resolve-library-id` → `query-docs`) for the current OpenAI and Google IDs. Update defaults in `config.py` **and** `.env.example` together. Keep `OPENAI_FALLBACK_MODEL` for now (removed in Task 3).
- [ ] **Step 5:** Test: `tests/test_config.py::test_default_models_match_env_example` parses `.env.example` and asserts every `KEY=value` that maps to a `Settings` field equals the default in `Settings()` (catches drift permanently).
- [ ] **Step 6:** CI: `astral-sh/setup-uv` (check latest major), matrix Python 3.11/3.12/3.13, steps `uv sync --locked`, `uv run ruff check .`, `uv run mypy src` (start with `check_untyped_defs = true`; if mypy is noisy on current code, fix trivial issues and add narrowly-scoped `# type: ignore[code]` only where unavoidable — no blanket ignores), `uv run pytest`.
- [ ] **Step 7:** Docs: replace every `pip install -r requirements.txt` / venv instruction in README, CONTRIBUTING, CLAUDE.md with `uv sync` and `uv run …` (the run command stays `uv run python app.py` until Task 2 adds the console script). Each command is documented once in its owner file; CLAUDE.md just lists commands.

**Acceptance:** Fresh clone → `uv sync && uv run pytest && uv run ruff check . && uv run mypy src` all pass; `requirements.txt` gone; `uv lock --check` passes in CI.

---

### Task 2: Package restructure, output paths, docs skeleton

**Model:** Sonnet · **Depends on:** 1

**Why:** Every module has a `try: from .x / except ImportError: from x` dual import; the repo root *is* the package; output dirs are computed from `Path(__file__).parent` (breaks when pip-installed, and puts generated code + agent cwd next to `.env`). This is the most invasive mechanical change, so do it before feature work.

**Files:**
- `git mv` all modules into `src/idea_to_mvp/` per the target layout (`app.py→ui/app.py`, `submit_service.py→ui/service.py`, `render.py→ui/render.py`); move `tests/conftest.py` sys.path hack out (package is installed editable by uv).
- Create: `src/idea_to_mvp/__main__.py`, `docs/architecture.md`
- Modify: `pyproject.toml` (`[build-system]` hatchling, `[tool.hatch.build.targets.wheel] packages`, `[project.scripts] idea-to-mvp = "idea_to_mvp.ui.app:main"`), all imports, `config.py`, `.gitignore`, `CLAUDE.md`, `README.md`, `CONTRIBUTING.md`, `.github/workflows/ci.yml`

**Interfaces (produces):**
```python
# config.py
class Settings(BaseSettings):
    output_dir: Path = Field(default=Path("~/idea-to-mvp").expanduser())   # env OUTPUT_DIR
    @property
    def exports_dir(self) -> Path: ...      # output_dir / "exports"
    @property
    def blueprints_dir(self) -> Path: ...   # output_dir / "blueprints"
    @property
    def projects_dir(self) -> Path: ...     # output_dir / "projects"
```
`agents._project_bundle_root()` / `_generated_projects_root()` / `SubmitService._project_dir` are replaced by these properties. Default is **outside the repo** so agents never run next to `.env`.

- [ ] **Step 1:** Write `tests/test_paths.py`: `Settings(output_dir=tmp_path).blueprints_dir == tmp_path/"blueprints"`; `save_session_markdown` writes under `settings.exports_dir`. Run → fail.
- [ ] **Step 2:** `git mv` files; rewrite imports to absolute `from idea_to_mvp.x import y` (no relative-dual pattern anywhere: `grep -rn "except ImportError" src` must return nothing except the optional `markdown` import).
- [ ] **Step 3:** Implement the path properties, thread `settings` into `plan_bundle_node`, `implementer_node`, `SubmitService`, `save_session_markdown(exports_dir=...)`. Update existing test monkeypatches (`agents._project_bundle_root`) to set `output_dir`.
- [ ] **Step 4:** Add `__main__.py` (`from idea_to_mvp.ui.app import main; main()`). Verify `uv run idea-to-mvp` and `uv run python -m idea_to_mvp` both launch.
- [ ] **Step 5:** Create `docs/architecture.md` (pipeline diagram in prose, state contract table, module map, "Import & test conventions"). Move the architecture content out of `CLAUDE.md` and README's "Project Layout" into it; leave `CLAUDE.md` with commands + conventions + a link. README keeps only the pipeline overview and links.
- [ ] **Step 6:** Existing local data: document a one-line migration in CHANGELOG (move `exports/`, `project_blueprints/` → `blueprints/`, `generated_projects/` → `projects/` into the new `OUTPUT_DIR`). Keep the three old dirs in `.gitignore` so leftover local data never shows up as untracked.

**Acceptance:** All 42 tests pass with only import/path edits; `uv run idea-to-mvp` works; no dual-import blocks remain; docs have no duplicated architecture text.

---

### Task 3: Split `agents.py`; remove provider hacks; shared state factory; graph-diagram sync test

**Model:** Sonnet (Opus if the split gets tangled) · **Depends on:** 2

**Why:** `agents.py` (829 lines) mixes provider abstraction, retry hacks, prompt assembly, 14 nodes, gate parsing. `discussion_node` contains ~50 lines of OpenAI-only retry logic; `_infer_provider_from_model` treats any model starting with `"o"` as OpenAI; `Settings` ↔ `ROLES` are wired by attribute-name strings; the initial state is duplicated in `submit_service._initial_state` and `tests/test_agents_routing._base_state`.

**Files:**
- Create: `llm/__init__.py`, `llm/runtime.py` (LlmRuntime/AgentRuntime/`get_runtime`/`clear_runtime_caches`), `llm/providers.py` (`build_llm`), `llm/invoke.py`, `nodes/{discussion,summary,architecture,strategy,blueprint,implement,verify,report,gates}.py`, `scripts/gen_graph_diagram.py`, `tests/test_docs_sync.py`
- Modify: `graph.py`, `state.py` (add `make_initial_state`), `ui/service.py`, `tests/*`
- Delete: `agents.py` (leave no compat shim; update the monkeypatch targets documented in `CLAUDE.md`)

**Interfaces (produces):**
```python
# llm/invoke.py
def invoke_text(runtime: LlmRuntime, messages: list[BaseMessage], *, max_tokens: int | None = None) -> str: ...
    # returns extracted text ('' if empty); owns finish-reason / empty-output retry
# llm/providers.py
def build_llm(provider: Provider, model: str, max_tokens: int, settings: Settings) -> BaseChatModel: ...
# state.py
def make_initial_state(idea: str, rounds: int) -> IdeaDiscussionState: ...
```

- [ ] **Step 1:** Write `tests/test_llm_invoke.py`: empty response from a fake model triggers exactly one retry with the "visible final answer" instruction, then returns `''`; Google `MAX_TOKENS` + incomplete text retries with doubled `max_output_tokens`. Run → fail.
- [ ] **Step 2:** Implement `invoke_text`, generalising today's Google retry and OpenAI empty-output retry into provider-agnostic "empty output → retry once with stricter instruction". Delete the hand-rolled `OPENAI_FALLBACK_MODEL` branch and setting (use `llm.with_fallbacks([...])` only if `OPENAI_FALLBACK_MODEL` is still set — otherwise drop it and note removal in CHANGELOG).
- [ ] **Step 3:** Replace `_infer_provider_from_model` with a strict mismatch **warning** only when the model name contains another provider's marker (`claude`, `gemini`, `gpt-`); never auto-rewrite the provider.
- [ ] **Step 4:** Move node functions into `nodes/*.py` (one file per pipeline concern; each exports its node + router). `graph.py` imports from `nodes`. Behaviour identical.
- [ ] **Step 5:** `make_initial_state`; replace both duplicates; add `test_initial_state_has_all_state_keys` asserting `set(make_initial_state("x",1)) == set(IdeaDiscussionState.__annotations__)`.
- [ ] **Step 6:** `scripts/gen_graph_diagram.py` writes `build_graph(False).get_graph().draw_mermaid()` between `<!-- graph:start -->`/`<!-- graph:end -->` markers in `docs/architecture.md`; `tests/test_docs_sync.py` regenerates in-memory and asserts equality.

**Acceptance:** No file in `src/idea_to_mvp/nodes` > ~250 lines; `grep -n "isinstance(.*ChatOpenAI" src` empty; tests green; diagram in docs matches the graph.

---

### Task 4: Offline demo mode + end-to-end harness

**Model:** Opus · **Depends on:** 3

**Why:** Right now nobody can try the project without three API keys and real spend, CI can't exercise the full flow, and the README has no screenshots. A deterministic offline mode is the single biggest boost to OSS adoption and CV credibility, and it makes every later task cheap to verify.

**Files:**
- Create: `src/idea_to_mvp/demo/__init__.py`, `demo/models.py` (`DemoChatModel`), `demo/fixtures.py`, `demo/implementer.py`, `tests/test_e2e_demo.py`
- Modify: `config.py` (`demo_mode: bool = False`, env `DEMO_MODE`), `llm/runtime.py` (`get_runtime` returns `DemoChatModel` runtimes when `demo_mode`), `implementer.py` (dispatch to demo implementer), `ui/app.py` (banner "Demo mode — canned outputs, no API calls"), `.env.example`, `README.md`, `CONTRIBUTING.md`

**Interfaces (produces):**
```python
class DemoChatModel(BaseChatModel):
    role: str
    def _generate(self, messages, stop=None, run_manager=None, **kw) -> ChatResult: ...
        # picks canned output by self.role via fixtures.RESPONSES[role](messages)
def demo_prepare_and_implement(workspace: Path, strategy: dict) -> str: ...
    # writes a tiny real Python project (module + passing pytest test + README) and returns a summary
```
Fixture lookup is by `role` (`pm`, `tech_lead`, `skeptic`, `summarizer`, `architect`, `strategy`, `planner`, plus every later role), returning idea-aware text (echo a slice of the user idea) so the demo feels real. Strategy returns valid JSON; questions return 5 numbered lines.

- [ ] **Step 1:** Write `tests/test_e2e_demo.py` (with `DEMO_MODE=true`, `output_dir=tmp_path`, checkpointer on): drive `SubmitService.handle_submit` through idea → answers → arch choice → plan gate → implement gate; assert final mode `done`, `delivery_report` mentions a workspace that exists, `plan.md` exists in the blueprint dir, verification passed. Run → fail.
- [ ] **Step 2:** Implement `DemoChatModel` + fixtures + demo implementer/verifier (verifier in demo mode runs `python -m pytest -q` in the demo workspace via subprocess and parses the exit code).
- [ ] **Step 3:** `make_ui()` shows the demo banner; `DEMO_MODE` documented in `.env.example` (only place).
- [ ] **Step 4:** Add the e2e test to CI (it runs by default; it is fast and offline).

**Acceptance:** With no API keys, `DEMO_MODE=true uv run idea-to-mvp` walks the entire pipeline to a delivered demo project; `pytest tests/test_e2e_demo.py` passes in < 20 s.

---

# Phase 1 — Orchestration core

### Task 5: Typed outputs between agents

**Model:** Opus · **Depends on:** 4

**Why:** Agents currently pass prose to each other and the code recovers structure with regexes (`_extract_questions`, `_ensure_questions`, `_strip_json_fences` + hand JSON parsing for strategy). Fragile, untestable, and the UI can only show markdown walls. Also `planner_offer_node` spends an LLM call to phrase a yes/no question whose text is static.

**Files:**
- Create: `src/idea_to_mvp/schemas.py`, `llm/structured.py`, `tests/test_schemas.py`, `tests/test_structured.py`
- Modify: `nodes/summary.py`, `nodes/architecture.py`, `nodes/strategy.py`, `nodes/gates.py`, `graph.py` (drop `planner_offer` node), `state.py`, `roles.py` (prompts stop describing output formats; schemas do), `blueprints.py` (`build_bundle_context`), `ui/service.py` + `ui/render.py` (render from typed data), `demo/fixtures.py`, docs

**Interfaces (produces):**
```python
# schemas.py
class MvpQuestion(BaseModel):
    question: str = Field(max_length=200)
    why_it_matters: str
    suggested_answer: str            # used by autopilot (Task 16) and answer form (Task 17)
class QuestionSet(BaseModel):
    questions: list[MvpQuestion] = Field(min_length=5, max_length=5)

class ArchitectureOption(BaseModel):
    key: Literal["A", "B"]
    name: str
    style: str
    stack: list[str]
    persistence: str
    integrations: list[str]
    security_baseline: str
    tradeoffs: list[str]
    limits: str
class ArchitectureProposal(BaseModel):
    option_a: ArchitectureOption
    option_b: ArchitectureOption
    shared_components: list[str]
    recommendation: Literal["A", "B"]
    recommendation_rationale: str
    biggest_tradeoff: str
    rollout: list[str]               # MVP phase -> scale-up phase

class Workstream(BaseModel): name: str; focus: str; deliverables: str
class ExecutionStrategy(BaseModel):
    mode: Literal["subagents", "agent_team"]
    reasoning: str
    workstreams: list[Workstream] = Field(min_length=1, max_length=5)

# llm/structured.py
def invoke_structured(runtime: LlmRuntime, messages: list[BaseMessage], schema: type[T], *, fallback: Callable[[], T] | None = None) -> T: ...
```
`invoke_structured`: `runtime.llm.with_structured_output(schema)`; on `ValidationError`/parse failure retry once appending the validation error text; then call `fallback()` (deterministic default, e.g. today's fallback questions/strategy) or raise `StructuredOutputError`. State keeps `model_dump()` dicts (`questions`, `architecture_proposal`, `execution_strategy`) plus rendered markdown (`architecture` string kept for prompts via `render_architecture_markdown(proposal)`).

- [ ] **Step 1:** Write failing tests: `QuestionSet` rejects 4 questions; `invoke_structured` retries once with error feedback then uses fallback (fake runtime whose `with_structured_output` raises); `render_architecture_markdown` contains both option names and the recommendation.
- [ ] **Step 2:** Implement schemas + `invoke_structured` (+ demo fixtures returning valid schema instances — extend `DemoChatModel` so `with_structured_output(schema)` returns a fixture instance).
- [ ] **Step 3:** Convert summarizer questions, architect, strategy to structured calls. Keep summary as free markdown. Delete regex helpers and `_parse_strategy`/`_strip_json_fences`; port their tests to schema tests.
- [ ] **Step 4:** Remove the `planner_offer` node and `PLAN_OFFER_SYSTEM` role; `plan_gate_node` uses a constant question string. Update `state.py`, `graph.py`, `test_agents_routing`.
- [ ] **Step 5:** UI renders questions/architecture from typed data (still markdown/HTML cards; the richer forms arrive in Task 17). Interrupt payloads now carry the dicts (`questions: list[dict]`, `proposal: dict`) alongside the old strings so the UI change stays small.
- [ ] **Step 6:** Regenerate the graph diagram; docs: `docs/architecture.md` state contract + "structured outputs" pattern note; CHANGELOG.

**Acceptance:** No regex parsing of LLM output remains for questions/architecture/strategy; malformed model output degrades to fallback with a logged warning; one fewer LLM call per run.

---

### Task 6: Async runtime, durable persistence, session resume, state-derived UI

**Model:** Opus · **Depends on:** 5

**Why:** `MemorySaver` loses everything on restart — a crashed multi-hour implementation run cannot resume. The UI keeps four parallel copies of state in `gr.State` (`thread_id`, `mode`, `turns_state`, `transcript_state`, `chat_state`) that can drift from the graph. Parallel work (later tasks) needs the async runtime anyway.

**Files:**
- Create: `src/idea_to_mvp/sessions.py`, `src/idea_to_mvp/ui/view.py`, `tests/test_sessions.py`, `tests/test_view.py`
- Modify: `pyproject.toml` (add `langgraph-checkpoint-sqlite`, `aiosqlite`), `config.py` (`checkpoint_db: Path | None` default `output_dir/sessions.db`; `ENABLE_CHECKPOINTER` removed → `CHECKPOINTER=sqlite|memory|off`), `graph.py`, `ui/service.py`, `ui/app.py`, `exporter.py`, tests

**Interfaces (produces):**
```python
# graph.py
@asynccontextmanager
async def open_graph(settings: Settings) -> AsyncIterator[CompiledStateGraph]: ...   # AsyncSqliteSaver lifecycle
# sessions.py
@dataclass(frozen=True)
class SessionInfo: thread_id: str; title: str; created_at: datetime; updated_at: datetime; stage: str; interrupted_at: str | None
class SessionRegistry:
    def __init__(self, db_path: Path): ...
    def upsert(self, thread_id: str, title: str, stage: str) -> None: ...
    def list(self, limit: int = 50) -> list[SessionInfo]: ...
    def delete(self, thread_id: str) -> None: ...
# ui/view.py  (pure functions, no Gradio)
@dataclass(frozen=True)
class ViewEntry: kind: str; speaker: str; content: str
def transcript_from_state(values: dict, pending_interrupt: dict | None) -> list[ViewEntry]: ...
def mode_from_state(values: dict, pending_interrupt: dict | None) -> str: ...   # replaces MODE_* tracking in gr.State
```

- [ ] **Step 1:** Failing tests: (a) `transcript_from_state` on a checkpoint-shaped dict yields discussion turns, summary, questions, architecture in order; `mode_from_state` returns `answers` when the pending interrupt kind is `answers`. (b) `SessionRegistry` round-trips and orders by `updated_at`. (c) an async test compiles the graph with a temp SQLite file, runs to the first interrupt, **rebuilds the graph from a new saver instance**, and resumes with `Command(resume=...)` successfully.
- [ ] **Step 2:** `open_graph` with `AsyncSqliteSaver.from_conn_string`; keep an in-memory option for tests; nodes stay sync (LangGraph runs them in threads) except where later tasks need async.
- [ ] **Step 3:** Rewrite `SubmitService` to `async def` generators using `graph.astream(..., stream_mode="updates")`; after every step derive the chat from `graph.aget_state(config)` via `ui/view.py` instead of mutating parallel lists. Delete `turns_state`, `transcript_state`, `chat_state`; only `thread_id` remains in `gr.State`. Mode comes from `mode_from_state`.
- [ ] **Step 4:** UI: "Sessions" dropdown (title · stage · time) + "Resume" and "Delete"; on load, list sessions; resuming re-renders the transcript from the checkpoint and shows the right gate.
- [ ] **Step 5:** Exporter takes `list[ViewEntry]` (state-derived) instead of `transcript_state` dicts; keep output format.
- [ ] **Step 6:** Regenerate diagram (unchanged topology, still verify); `docs/architecture.md` gets "Persistence & resume" section; `.env.example` `CHECKPOINTER`/`CHECKPOINT_DB`; CHANGELOG.

**Acceptance:** Kill the process at any gate, restart, pick the session, and continue; the UI code has no copy of pipeline state; e2e demo test still green.

---

### Task 7: Resilience, usage/cost tracking, observability, `doctor`

**Model:** Sonnet · **Depends on:** 6

**Why:** No node has a retry policy or timeout; a transient 429 mid-run aborts the stage. Token usage is logged but not aggregated; only the Agent SDK cost is visible in logs. Users can't tell whether keys/models are valid until a stage fails.

**Files:**
- Create: `src/idea_to_mvp/usage.py`, `src/idea_to_mvp/doctor.py`, `tests/test_usage.py`, `tests/test_doctor.py`
- Modify: `state.py` (`usage: Annotated[list[UsageRecord], operator.add]`), `graph.py` (`RetryPolicy` on LLM nodes), `llm/invoke.py` (records usage), `config.py`, `pyproject.toml` (`idea-to-mvp doctor` subcommand), `.env.example`, `SECURITY.md`? (no), `README.md` (one troubleshooting line), `docs/architecture.md`

**Interfaces (produces):**
```python
# usage.py
class UsageRecord(TypedDict):
    role: str; provider: str; model: str
    input_tokens: int; output_tokens: int; cost_usd: float | None   # cost_usd only for SDK sessions
def summarize_usage(records: list[UsageRecord]) -> dict[str, Any]: ...   # totals + per-role
# graph.py: retry policy applied via builder.add_node(name, fn, retry_policy=LLM_RETRY)
LLM_RETRY = RetryPolicy(max_attempts=3, initial_interval=2.0, backoff_factor=2.0, retry_on=_is_transient)
```
`_is_transient` returns True for rate-limit/timeout/5xx/connection errors by exception class name (no provider-specific imports), False for auth/validation errors.

- [ ] **Step 1:** Failing tests: `_is_transient(RateLimitError-like)` True, auth False; a node that raises transient twice then succeeds is retried (LangGraph test with a fake node); `summarize_usage` aggregates per role; `doctor` with fake providers reports "OK"/"FAIL: invalid key" per role without raising.
- [ ] **Step 2:** Add per-request `timeout` (`LLM_TIMEOUT_SECONDS`, default 120) and `max_retries=2` to `build_llm`; apply `RetryPolicy` to LLM nodes.
- [ ] **Step 3:** `invoke_text`/`invoke_structured` return usage via `usage_metadata`; nodes append `UsageRecord`s to state. SDK sessions (later tasks) append `cost_usd`.
- [ ] **Step 4:** `idea-to-mvp doctor`: for each configured role, a 1-token ping via the role's runtime; also checks `claude` CLI availability for the implementer and prints a table. Exit code 1 on any failure.
- [ ] **Step 5:** Optional tracing: if `LANGSMITH_TRACING=true` is set, document it (no code needed beyond passing `run_name`/`tags` in `graph.astream(config=...)`: `{"run_name": "idea-to-mvp", "tags": [thread_id]}`).
- [ ] **Step 6:** UI: a small "tokens / $" badge in the status area reading `summarize_usage(state["usage"])` (plain text; the polished version is Task 16).

**Acceptance:** Transient provider errors self-heal; `uv run idea-to-mvp doctor` gives a clear pass/fail per role; usage totals visible after a demo run.

---

### Task 8: Parallel, moderated panel (subgraph)

**Model:** Opus · **Depends on:** 6, 7

**Why:** The panel is a strictly sequential 3×N loop with fixed order and a fixed length; each speaker waits for the others even for the first, independent opening statements; the loop can't end early when the panel has converged.

**Files:**
- Create: `nodes/panel.py` (subgraph builder), `tests/test_panel.py`
- Modify: `state.py` (`opening_turns`, `panel_mode`, `convergence`), `roles.py` (add `moderator` role + prompt), `config.py` (`panel_mode: Literal["moderated","round_robin"]`, `panel_max_rounds` cap semantics unchanged, `moderator_model`), `graph.py`, `nodes/discussion.py`, `ui/view.py`, `demo/fixtures.py`, `.env.example`, `docs/architecture.md`, `README.md` (pipeline block)

**Interfaces (produces):**
```python
class ModeratorDecision(BaseModel):
    converged: bool
    next_speaker: Literal["PM", "Tech Lead", "Skeptic"] | None
    reason: str          # shown in the UI as the convergence note
# subgraph: opening (Send x3, parallel) -> merge_openings (canonical PM, Tech Lead, Skeptic order)
#           -> [moderator -> speaker turn]* until converged or turn cap -> END
def build_panel_subgraph() -> CompiledStateGraph: ...
```
- [ ] **Step 1:** Failing tests: openings run for all three speakers and land in history in canonical order regardless of completion order (fake runtimes with different sleeps); moderator `converged=True` ends the loop before the cap; cap still ends it; `panel_mode="round_robin"` reproduces today's turn order exactly.
- [ ] **Step 2:** Implement `opening_turn` node fanned out with `Send("opening_turn", {"speaker": s, ...})`; results in `opening_turns: dict[str,str]` (dict-merge reducer); `merge_openings` appends `AIMessage`s in canonical order.
- [ ] **Step 3:** Moderator: cheap structured call after each turn from round 2 on; rules — never converge before every speaker has spoken twice; never pick the same speaker twice in a row; hard cap = `max_rounds * 3`.
- [ ] **Step 4:** Mount the subgraph as a single node in the parent graph (`builder.add_node("panel", build_panel_subgraph())`); stream with `subgraphs=True` so the UI still sees per-turn updates.
- [ ] **Step 5:** Cap parallel LLM calls: pass `config={"max_concurrency": settings.llm_max_concurrency}` (new setting, default 4) from the run-config helper used by `SubmitService`.
- [ ] **Step 6:** Docs: diagram regen, README pipeline line ("opening statements in parallel, moderator ends the debate when it converges"), CHANGELOG.

**Acceptance:** A 3-round panel finishes its opening round in ~1 call-latency instead of 3; the debate can stop early with a visible reason.

---

### Task 9: Parallel blueprint DAG (fixes plan-can't-see-PRD)

**Model:** Opus · **Depends on:** 5, 8

**Why:** `create_project_bundle` generates 8 + N documents strictly one after another, each from the same context block, so `plan.md` (which must cite PRD requirement IDs `R1..`) never sees the actual PRD, and subagent definitions never see the plan. It's both slow and a correctness gap.

**Files:**
- Create: `nodes/blueprint_graph.py` (subgraph), `tests/test_blueprint_dag.py`
- Modify: `blueprints.py`, `nodes/blueprint.py`, `state.py` (`blueprint_docs: Annotated[dict[str,str], merge_dicts]`), `graph.py`, `demo/fixtures.py`, `docs/architecture.md`, `CHANGELOG.md`

**Interfaces (produces):**
```python
@dataclass(frozen=True)
class BundleFileSpec:
    relative_path: str; system_prompt: str; instruction: str; fallback: str
    frontmatter: str = ""
    wave: int = 1                       # 1: PRD, ARCHITECTURE · 2: README, AGENTS x4, plan · 3: .claude/agents/*
    needs: tuple[str, ...] = ()         # upstream docs injected into this doc's prompt
def bundle_file_plan(strategy) -> list[BundleFileSpec]: ...
def prompt_for(spec: BundleFileSpec, context_block: str, generated: dict[str, str]) -> str: ...
def write_bundle(*, root: Path, user_idea: str, docs: dict[str, str], strategy: dict | None) -> tuple[Path, list[str], str]: ...
    # deterministic writer; replaces the generate-while-writing loop in create_project_bundle
```
`blueprints.py` stays LLM-free.

- [ ] **Step 1:** Failing tests: (a) `plan.md`'s prompt contains the generated PRD text and ARCHITECTURE text (fake generator captures prompts); (b) subagent docs' prompts contain `plan.md`; (c) within a wave all docs are requested concurrently (fake generator with a barrier); (d) `write_bundle` writes fallbacks for empty docs and `STRATEGY.json`.
- [ ] **Step 2:** Assign `wave`/`needs` in `bundle_file_plan`; implement `prompt_for` (append upstream docs under `## Upstream document: <path>` headings, truncated sensibly per doc with a `[truncated]` marker).
- [ ] **Step 3:** Subgraph: `wave_1` → `wave_2` → `wave_3` → `write`, each wave a `Send` fan-out over that wave's specs writing `{path: content}` into `blueprint_docs`.
- [ ] **Step 4:** Remove `create_project_bundle`; keep `build_bundle_context`. Update `plan_bundle_node` to use the subgraph.
- [ ] **Step 5:** Docs/diagram/CHANGELOG; measured before/after wall-clock in demo mode is not meaningful — instead assert concurrency in the test.

**Acceptance:** Blueprint generation is 3 sequential waves instead of ~12 sequential calls; `plan.md` references real R-IDs from the generated PRD.

---

### Task 10: Machine-readable plan, validators, critic/revise loop

**Model:** Opus · **Depends on:** 9

**Why:** `plan.md` is prose; the implementer can only tell an agent "read plan.md and do it". Parallel execution, per-task resume, progress boards, and verification all need a task graph. Plans are also never checked for consistency (cycles, dangling contract IDs, requirements no task covers).

**Files:**
- Create: `src/idea_to_mvp/plan.py`, `src/idea_to_mvp/blueprint_review.py`, `tests/test_plan.py`, `tests/test_blueprint_review.py`
- Modify: `blueprints.py` (plan spec becomes structured; `plan.json` + rendered `plan.md`), `nodes/blueprint_graph.py` (critic + revise loop), `roles.py` (`plan_writer`, `blueprint_critic`), `state.py`, `config.py` (`max_blueprint_revisions: int = 1`), `demo/fixtures.py`, docs

**Interfaces (produces):**
```python
# plan.py
class Contract(BaseModel): id: str; name: str; description: str
class PlanTask(BaseModel):
    id: str                       # "T01"
    title: str; goal: str
    workstream: str
    depends_on: list[str] = []
    requirement_ids: list[str]    # "R1"...
    contracts_in: list[str] = []; contracts_out: list[str] = []
    scope: str
    acceptance: list[str]; tests: list[str]
    coverage_target: int = 80
    handoff: str = ""
class ProjectCommands(BaseModel): install: str | None; test: str; lint: str | None; run: str | None
class Plan(BaseModel):
    contracts: list[Contract]; tasks: list[PlanTask]; commands: ProjectCommands
def validate_plan(plan: Plan, *, prd_markdown: str, workstreams: list[str]) -> list[str]: ...
    # issues: duplicate ids, unknown depends_on, cycles (graphlib.TopologicalSorter), unknown workstream,
    # unknown contract ids, requirement ids absent from the PRD, P0 requirements no task covers, empty test command
def execution_waves(plan: Plan) -> list[list[str]]: ...      # topological layers of task ids
def render_plan_markdown(plan: Plan) -> str: ...             # same field layout the old prompt produced
# blueprint_review.py
class Issue(BaseModel): severity: Literal["blocker","warning"]; file: str; description: str
class CritiqueReport(BaseModel): approved: bool; issues: list[Issue]
```
- [ ] **Step 1:** Failing tests for `validate_plan` (one per issue kind, incl. a 3-node cycle), `execution_waves` on a diamond DAG → `[[T01],[T02,T03],[T04]]`, `render_plan_markdown` snapshot.
- [ ] **Step 2:** Planner produces `Plan` via `invoke_structured` (wave 2, needs PRD+ARCHITECTURE); deterministic `validate_plan` result is fed back for one automatic repair attempt before the critic runs.
- [ ] **Step 3:** Critic node: structured cross-document review (PRD ↔ ARCHITECTURE ↔ plan ↔ subagent prompts). If not approved and `revisions < max_blueprint_revisions`, regenerate **only** documents named in blocker issues with the issues appended to their prompt, then re-validate. Remaining warnings are written to `REVIEW.md` in the bundle and shown in the plan gate.
- [ ] **Step 4:** Bundle now contains `plan.json` (source of truth) and `plan.md` (rendered). `STRATEGY.json` workstream names must equal the plan's workstreams (validated).
- [ ] **Step 5:** Docs (`docs/architecture.md`: "Blueprint pack" section describing files + evaluator-optimizer loop), diagram regen, CHANGELOG.

**Acceptance:** Every generated pack has a validated DAG; invalid plans are repaired or flagged before the user is asked to approve them.

---

# Phase 2 — Implementation, sandbox, parallel execution

### Task 11: Agent sandbox and workspace hygiene

**Model:** Opus · **Depends on:** 2, 6

**Why (security, highest priority):** Docs claim agents have "file and shell access inside `generated_projects/<project>/` only", but `cwd` is not a sandbox. With `permission_mode="bypassPermissions"` an agent can run any command anywhere the user can — including reading the repo's `.env` two directories up (Task 2 moves outputs away from it, but the hole remains), `~/.ssh`, and exfiltrating over the network. Also, workspaces copy `node_modules`, and `_workspace_file_tree` hides it only in the report.

**Files:**
- Create: `src/idea_to_mvp/implementation/__init__.py`, `implementation/options.py`, `implementation/guard.py`, `implementation/workspace.py`, `tests/test_guard.py`, `tests/test_workspace.py`
- Modify: `implementer.py` (uses `build_agent_options`), `nodes/implement.py`, `config.py`, `.env.example`, `SECURITY.md` (threat model, owner of this knowledge), `README.md` (remove the inaccurate claim; one-line pointer to SECURITY.md), `docs/architecture.md`

**Interfaces (produces):**
```python
# options.py
def build_agent_options(*, workspace: Path, settings: Settings, max_turns: int,
                        agents: dict | None = None, max_budget_usd: float | None = None) -> ClaudeAgentOptions: ...
    # single place that sets cwd, model, budget, sandbox=SandboxSettings(...), hooks, env scrub, tools
# guard.py
def check_tool_use(tool_name: str, tool_input: dict, workspace: Path) -> tuple[bool, str]: ...   # (allowed, reason)
async def pre_tool_use_hook(input_data, tool_use_id, context) -> HookJSONOutput: ...              # wraps check_tool_use
# workspace.py
def prepare_workspace(bundle_dir: Path, projects_root: Path) -> Path: ...    # copies bundle, `git init`, .gitignore, initial commit "blueprint"
def workspace_tree(workspace: Path, limit: int = 60) -> str: ...             # moved from nodes/report
```
Settings: `implementer_sandbox: Literal["auto","on","off"] = "auto"`, `implementer_permission_mode = "acceptEdits"` (new default), `implementer_allowed_domains: list[str]` default package registries (`pypi.org`, `files.pythonhosted.org`, `registry.npmjs.org`, `github.com`).

- [ ] **Step 1:** Failing tests for `check_tool_use`: Read/Write/Edit with a path outside the workspace (including `../` and symlink-escape via `realpath`) → denied; inside → allowed; Bash containing `sudo`, `rm -rf /`, `rm -rf ~`, `curl … | sh`, `git push`, `ssh`, `git config --global`, `cat ~/.ssh`, `printenv`/`env` piped to network tools → denied; ordinary `npm test`, `pytest`, `git commit` → allowed.
- [ ] **Step 2:** Implement `guard.py` as a defence-in-depth denylist (documented as such — the OS sandbox is the primary control).
- [ ] **Step 3:** `build_agent_options`: enable `SandboxSettings(enabled=True, autoAllowBashIfSandboxed=True, allowUnsandboxedCommands=False, network=...allowed domains...)` when `implementer_sandbox != "off"` (`"auto"`: enable if the platform supports it; fall back with a loud log warning); register `HookMatcher(matcher="Bash|Write|Edit|Read|MultiEdit", hooks=[pre_tool_use_hook])`; set `env` so `OPENAI_API_KEY`/`GOOGLE_API_KEY` and any `*_TOKEN` are blanked for the agent process (**verify empirically** with a `@pytest.mark.live` test that asks the agent to print its environment; if the SDK merges rather than replaces env, blank via explicit empty strings). Keep `bypassPermissions` selectable but the implement gate must show a red warning.
- [ ] **Step 4:** `workspace.py`: copy bundle **excluding** `node_modules`, `.venv`, `coverage`, `dist`; `git init`, `.gitignore`, first commit. All later commits are made by the orchestrator, not by the agents' prompts.
- [ ] **Step 5:** Rewrite the implement-gate cost/safety text to state sandbox status truthfully (`sandbox: on|off`, permission mode, allowed domains).
- [ ] **Step 6:** `SECURITY.md`: threat model (what agents can do, what is blocked, what is *not* protected, recommended container/VM for untrusted ideas). README/.env.example claims corrected.

**Acceptance:** A live smoke test (opt-in) proves an agent cannot read a file outside its workspace or see non-Anthropic API keys; workspaces are git repos with a clean `.gitignore`.

---

### Task 12: Task-by-task implementation engine with live events

**Model:** Opus · **Depends on:** 10, 11

**Why:** Today the lead session gets "implement the whole plan.md" with up to 120 turns and one $10 cap; it's opaque (the UI shows "this can take a while" for tens of minutes), unresumable, and cost accounting is invisible. One fresh-context session per plan task is more reliable, resumable at task granularity, and is the unit parallelism needs.

**Files:**
- Create: `implementation/executor.py`, `implementation/events.py`, `implementation/progress.py`, `tests/test_executor.py`, `tests/test_events.py`
- Modify: `implementer.py` (keeps lead+subagents mode), `nodes/implement.py` (async node, emits events), `state.py` (`task_results`), `ui/service.py` (subscribe to custom events), `demo/implementer.py`, `config.py`, docs

**Interfaces (produces):**
```python
# events.py
class ImplEvent(TypedDict):
    kind: Literal["task_start","tool","text","task_end","cost"]
    task_id: str | None; label: str; detail: str; cost_usd: float | None; ts: float
def events_from_sdk_message(message: Any, task_id: str | None) -> list[ImplEvent]: ...
# progress.py   (file: <workspace>/.idea-to-mvp/progress.json)
class TaskResult(TypedDict): task_id: str; status: Literal["done","failed","skipped"]; summary: str; cost_usd: float; turns: int; session_id: str | None; commit: str | None
def load_progress(workspace: Path) -> dict[str, TaskResult]: ...
def save_result(workspace: Path, result: TaskResult) -> None: ...
# executor.py
async def run_task(workspace: Path, task: PlanTask, settings: Settings, *, budget: "BudgetTracker",
                   emit: Callable[[ImplEvent], None]) -> TaskResult: ...
class BudgetTracker:                       # shared across sessions
    def __init__(self, total_usd: float): ...
    def remaining(self) -> float: ...
    def charge(self, usd: float) -> None: ...
```
Settings: `implementer_max_total_usd: float = 25.0` (whole-run cap; today's worst case is unbounded: per-session $10 × sessions), `implementer_max_task_usd = 5.0` (renamed from per-session), `implementer_max_task_turns = 40`.

- [ ] **Step 1:** Failing tests: `events_from_sdk_message` maps a fake `AssistantMessage` with a `ToolUseBlock(name="Write")` to a `tool` event; `run_task` (with an injected fake `query`) commits on success (`git log` has `T01: <title>`), writes progress, skips tasks already `done` on re-run, stops with `failed` when `BudgetTracker.remaining()` is 0; budget passed to the SDK = `min(max_task_usd, remaining)`.
- [ ] **Step 2:** Refactor `_run_agent_async` into an async generator yielding SDK messages; `run_task` consumes them, emits events, records `ResultMessage` cost/turns/session_id.
- [ ] **Step 3:** Task prompt template built from the `PlanTask` fields (goal, scope, acceptance, tests, contracts, dependencies' summaries from progress). After a task succeeds the orchestrator runs `git add -A && git commit`.
- [ ] **Step 4:** `implementer_node` becomes `async`; uses `langgraph.config.get_stream_writer()` to publish `ImplEvent`s; `SubmitService` streams with `stream_mode=["updates","custom"]` and appends progress lines to the status/console (rich console is Task 18).
- [ ] **Step 5:** Sequential mode for now (`for wave in execution_waves(plan): for task in wave: await run_task(...)`); `subagents` strategy still uses the lead session. Resume: re-running the node after a crash skips `done` tasks.
- [ ] **Step 6:** Append SDK cost to `usage` records (Task 7). Demo implementer emits a few synthetic events per task. Docs (`docs/architecture.md` "Implementation engine"), CHANGELOG, `.env.example`.

**Acceptance:** Killing the app mid-implementation and restarting resumes at the first unfinished task; the UI shows live per-task progress and a running $ total; a whole-run budget cap is enforced.

---

### Task 13: Parallel task execution in git worktrees

**Model:** Opus · **Depends on:** 12

**Why:** This is the headline "parallel execution" feature: independent plan tasks (different workstreams) run concurrently, each in its own git worktree/branch, merged back deterministically.

**Files:**
- Create: `implementation/scheduler.py`, `implementation/merge.py`, `nodes/implement_graph.py` (subgraph), `tests/test_scheduler.py`, `tests/test_merge.py`, `tests/test_implement_graph.py`
- Modify: `nodes/implement.py`, `state.py`, `roles.py` (strategy prompt: modes `subagents` | `agent_team` where `agent_team` now means DAG-parallel), `config.py`, `demo/implementer.py`, `.env.example`, `docs/architecture.md`, README (Execution strategies section)

**Interfaces (produces):**
```python
# scheduler.py
def next_ready(plan: Plan, done: set[str], running: set[str]) -> list[PlanTask]: ...   # deps satisfied, not running/done
# merge.py
async def create_worktree(workspace: Path, task_id: str) -> Path: ...          # git worktree add <ws>/../.worktrees/<id> -b task/<id>
async def merge_task(workspace: Path, task_id: str) -> MergeOutcome: ...      # rebase/merge into main; MergeOutcome(status: "merged"|"conflict", detail)
async def remove_worktree(workspace: Path, task_id: str) -> None: ...
```
Settings: `implementer_max_parallel: int = 3` (`1` = today's sequential behaviour).

- [ ] **Step 1:** Failing tests: diamond DAG schedules T02∥T03 only after T01; `merge_task` merges two branches that touch different files and reports `conflict` for the same line (real temp git repos); the subgraph never runs more than `implementer_max_parallel` tasks at once (fake `run_task` with an active-counter).
- [ ] **Step 2:** Subgraph `implement_plan`: `pick_wave` → `Send("run_task_node", task)` × ready tasks (respect max parallel via `max_concurrency` config) → `merge_wave` (merge in task-id order; each merge followed by the task's own test command from `plan.commands.test`) → loop until all tasks done/failed.
- [ ] **Step 3:** Conflict handling: on `conflict`, run one bounded `merge_resolver` agent session (prompt: resolve conflicts, keep both intents, run tests); if it fails, mark the task `failed` and continue with tasks not depending on it.
- [ ] **Step 4:** Each parallel task's SDK session uses the **worktree** as `cwd`/sandbox root (Task 11 options); tell the task prompt to run the install command if dependencies are missing (worktrees don't share `node_modules`/venvs).
- [ ] **Step 5:** Budget: shared `BudgetTracker` guards concurrent charges (`asyncio.Lock`); tasks stop starting when remaining < `implementer_max_task_usd`.
- [ ] **Step 6:** Strategy prompt updated; keep parsing `agent_team` for backward compatibility; the plan gate/implement gate payload includes `dag_width` (max wave size) and `task_count` for the UI estimate.
- [ ] **Step 7:** Docs + diagram + CHANGELOG. README "Execution strategies" rewritten in one short paragraph.

**Acceptance:** A plan with two independent workstreams demonstrably runs two SDK sessions simultaneously (visible in events), merges cleanly, and `IMPLEMENTER_MAX_PARALLEL=1` reproduces sequential behaviour.

---

### Task 14: Verification v2 — parallel lanes, structured verdict, graph-level fix loop

**Model:** Opus · **Depends on:** 12 (13 recommended)

**Why:** Verification is one agent reading README and printing a `VERDICT: PASS|FAIL` sentinel; a `while` loop inside `verifier_node` hides fix attempts from the graph (no checkpoint, no events); a missing sentinel silently becomes FAIL; nothing checks that PRD requirements are actually covered by tests.

**Files:**
- Create: `implementation/verify.py`, `tests/test_verify.py`
- Modify: `nodes/verify.py` (split into `verify`, `fix` nodes), `graph.py` (edges), `state.py` (`verification: VerificationResult` gains `lanes`), `implementer.py` (retire sentinel), `roles.py`/prompts, `demo/*`, `docs/architecture.md`

**Interfaces (produces):**
```python
class LaneReport(BaseModel):
    lane: Literal["tests","quality","requirements"]
    passed: bool
    commands_run: list[dict]      # {"command": str, "exit_code": int}
    failures: list[str]
    summary: str
class VerificationResult(TypedDict):
    passed: bool; attempts: int; report: str; lanes: list[dict]
# Agent SDK structured result: ClaudeAgentOptions(output_format={"type": "json_schema", "schema": LaneReport.model_json_schema()})
```
Lanes run concurrently via `Send`: **tests** (run `plan.commands.install/test`), **quality** (lint/type/security-obvious checks; includes a "does the documented run command start and answer a smoke probe" check where applicable), **requirements** (map every P0 `R#` to at least one test; report uncovered ones). Lane sessions are read-mostly (`disallowed_tools=["Edit","MultiEdit","Write"]` except tests lane may write coverage artefacts).

- [ ] **Step 1:** Failing tests: a lane result that is not valid JSON counts as FAIL with an explicit reason (not silently); overall `passed` requires all lanes; graph routes `verify → fix → verify` until `attempts == max_fix_attempts`, then to `delivery_report`; each attempt appears as its own stream event.
- [ ] **Step 2:** Implement lanes + structured output parsing (validate against `LaneReport`); demo verifier implements the same lane contract.
- [ ] **Step 3:** Graph edges: `verify` → conditional (`pass`|`exhausted` → `delivery_report`, `retry` → `fix`) → `verify`. `fix` receives the merged failure list, not the raw prose.
- [ ] **Step 4:** Delivery report includes per-lane verdicts (rendering polish is Task 18).
- [ ] **Step 5:** Docs/diagram/CHANGELOG (`docs/architecture.md`: "Verification lanes").

**Acceptance:** Three lanes run in parallel; every fix attempt is a visible, checkpointed graph step; uncovered P0 requirements are reported.

---

### Task 15: Iteration loop and delivery bundle

**Model:** Opus · **Depends on:** 13, 14

**Why:** An MVP is version one. After delivery the user should be able to say "add X / fix Y" and get v0.2 through the same machinery, instead of starting a new pipeline.

**Files:**
- Create: `nodes/iterate.py`, `delivery.py`, `tests/test_iterate.py`, `tests/test_delivery.py`
- Modify: `graph.py`, `state.py` (`iteration: int`, `change_requests: list[str]`), `plan.py` (`append_iteration_tasks`), `roles.py` (`change_planner`), `config.py` (`max_iterations: int = 5`), `demo/fixtures.py`, docs

**Interfaces (produces):**
```python
# interrupt payload / resume
{"kind": "iterate_gate", "report": str, "iteration": int}  ->  resume {"iterate": bool, "feedback": str}
# delivery.py
def make_delivery_zip(workspace: Path, dest_dir: Path) -> Path: ...     # excludes .git? (option), node_modules, .venv, coverage
def tag_iteration(workspace: Path, iteration: int) -> str: ...           # git tag v0.<n>
# nodes/iterate.py
class ChangePlan(BaseModel): tasks: list[PlanTask]      # ids prefixed "I<n>-01", depends_on may reference existing tasks
```
- [ ] **Step 1:** Failing tests: resume `{"iterate": False}` ends the graph; `{"iterate": True, "feedback": "..."}` routes to `change_planner` → `implement_plan` (only new tasks run; done tasks are skipped by progress) → `verify` → `delivery_report` → `iterate_gate` again; after `max_iterations` the gate is not offered; `make_delivery_zip` excludes `node_modules` and `.venv`.
- [ ] **Step 2:** Implement `change_planner` (structured `ChangePlan`, validated by `validate_plan` against the extended plan), append to `plan.json`/`plan.md` (no rewrite of finished tasks), tag `v0.<iteration>` after each verified delivery.
- [ ] **Step 3:** Delivery zip produced in `delivery_report` and referenced in state (`delivery_zip`).
- [ ] **Step 4:** Docs: `docs/architecture.md` "Iteration loop"; README pipeline gets the last line "🔁 Iterate: request changes, get v0.2"; diagram regen; CHANGELOG.

**Acceptance:** After a delivered demo project, a feedback message produces new tasks, a passing verification, tag `v0.2`, and an updated zip.

---

# Phase 3 — UI / UX

Current UX findings driving this phase: one shared textbox whose meaning changes per gate; five MVP questions answered in one free-text box; architecture options shown as two markdown walls plus an A/B radio; hard-coded dark-only colours (`#111827`…) that break light theme; fixed 700px chat; pre-filled default idea, no examples; no Stop button; no cost/time display; silent multi-minute implementation; no session history; no way to edit the blueprint before spending money; no download of the result; `_pack()` returns a 12-tuple that every handler must keep in sync.

### Task 16: UI shell, design system, autopilot, stop

**Model:** Opus · **Depends on:** 6, 7 (uses the Gradio 6 `launch(theme=, css=)` hook from Task 1)

**Files:**
- Create: `ui/theme.py`, `ui/components.py` (stepper, badges), `tests/test_ui_render.py`
- Modify: `ui/app.py`, `ui/render.py`, `ui/service.py`, `nodes/gates.py` (autopilot), `state.py` (`autopilot: bool`), `README.md` (screenshots come in Task 23), `docs/architecture.md` (UI section)

**Interfaces (produces):**
```python
# ui/theme.py
def build_theme() -> gr.themes.ThemeClass: ...     # works in light + dark
CSS: str                                            # CSS variables only (--card-bg, --accent-pm, ...); no raw hex outside :root
# ui/components.py
def stage_stepper(stage: str, statuses: dict[str, str], elapsed: dict[str, float]) -> str: ...
def usage_badge(summary: dict) -> str: ...
# gates.py (autopilot)
# auto-resume with recommended defaults for answers/arch/plan gates when state["autopilot"]; implement gate ALWAYS asks (cost)
```
- [ ] **Step 1:** Failing tests for `stage_stepper` (active/done/todo classes, elapsed formatting), `usage_badge`, and that `CSS` contains no hex colour outside `:root`/`[data-theme]` blocks.
- [ ] **Step 2:** Theme/CSS rewrite with variables; verify both colour schemes in the browser pane (resize/colorScheme emulation) and take screenshots for the record.
- [ ] **Step 3:** Layout: header with settings accordion (rounds, panel mode, autopilot, model profile display), sticky stepper with per-stage elapsed time and token/$ badge, responsive chat height (`height="70vh"`), example ideas via `gr.Examples` (4 diverse ideas; remove the pre-filled photographers text).
- [ ] **Step 4:** Stop button wired with Gradio `cancels=[run_event]`; the graph run is cancelled cooperatively (checkpoint keeps state; UI returns to the last gate/stage and offers resume).
- [ ] **Step 5:** Autopilot: answers = each question's `suggested_answer`, arch = `recommendation`, plan gate = generate; never auto-approves the implement gate. Toggle stored in state.
- [ ] **Step 6:** Docs + CHANGELOG.

**Acceptance:** Light and dark themes both readable; a demo run in autopilot stops only at the implement gate; Stop halts a run without corrupting the session.

---

### Task 17: Gate forms (answers, architecture compare, editable blueprint, implement estimate)

**Model:** Opus · **Depends on:** 5, 10, 16

**Files:**
- Create: `ui/gates.py`, `tests/test_gates_ui.py`
- Modify: `ui/app.py`, `ui/service.py` (delete `DECISION_CHOICES`, `_INPUT_LABELS`, `_BUTTON_LABELS`, and the per-mode if/elif in `handle_submit`), `ui/render.py`, docs

**Interfaces (produces):**
```python
@dataclass(frozen=True)
class GateSpec:
    kind: str                                   # "answers" | "arch_choice" | "plan_gate" | "implement_gate" | "iterate_gate"
    render: Callable[[dict], GateView]           # payload -> component updates
    build_resume: Callable[[GateInputs], Any]    # form inputs -> Command(resume=...) payload
GATES: dict[str, GateSpec]
```
- **Answers gate:** five labelled textboxes prefilled with `suggested_answer`, each showing `why_it_matters`; "Use all suggestions" button; assembled into the existing `"1. …\n2. …"` resume string.
- **Architecture gate:** two side-by-side cards (name, style, stack chips, tradeoffs, limits) with a "Recommended" badge and the rationale; radio + notes.
- **Blueprint gate:** file selector + `gr.Code` editor for `PRD.md`, `ARCHITECTURE.md`, `plan.md` (plan validation re-run on save; invalid edits blocked with the issue list); review warnings from Task 10 shown.
- **Implement gate:** summary table — model, sandbox status, permission mode, tasks, DAG width, parallelism selector (`Sequential | Parallel N`), total budget cap, estimated sessions; red banner if `bypassPermissions` or sandbox off.
- [ ] **Step 1:** Failing tests: each `GateSpec.build_resume` produces the payload the corresponding node expects (import the node's payload parser and round-trip); answers form with suggestions yields five numbered lines; editing `plan.md` into a cyclic plan is rejected.
- [ ] **Step 2–5:** Implement gates one by one (answers → architecture → blueprint → implement), replacing the shared textbox/radio. Keep `iterate_gate` a feedback textbox + "Iterate / Finish".
- [ ] **Step 6:** Delete the old mode dictionaries; the service dispatches through `GATES`. Docs (`docs/architecture.md`: "Adding a gate" = one `GateSpec`), CHANGELOG.

**Acceptance:** No gate reuses another gate's widgets; adding a new gate touches one node + one `GateSpec`; blueprint files are editable before spending money.

---

### Task 18: Live implementation console and delivery dashboard

**Model:** Opus · **Depends on:** 12, 13, 14, 16

**Files:**
- Create: `ui/console.py`, `ui/dashboard.py`, `tests/test_console.py`, `tests/test_dashboard.py`
- Modify: `ui/app.py`, `ui/service.py`, `ui/gates.py` (iterate form), docs

**Interfaces (produces):**
```python
def render_task_board(plan: dict, results: dict[str, TaskResult], running: set[str]) -> str: ...   # columns: pending | running | done | failed, with branch + cost
def render_console(events: list[ImplEvent], limit: int = 200) -> str: ...                            # collapsible, auto-scroll
def render_dashboard(state: dict) -> str: ...      # verdict per lane, cost/time, requirements coverage, run instructions
```
- [ ] **Step 1:** Failing tests on the three renderers with fabricated state/events (HTML contains task ids in the right column, lane verdict badges, escaped content — assert `<script>` in an event detail is escaped).
- [ ] **Step 2:** Implementation tab: task board + live console fed by `custom` stream events; running cost meter against the run budget; per-task expandable summary and `git diff --stat`.
- [ ] **Step 3:** Artifacts panel: `gr.FileExplorer` rooted at the workspace (read-only), download buttons for the delivery zip and blueprint folder zip, path text with copy button ("Open folder").
- [ ] **Step 4:** Delivery dashboard replaces the markdown delivery report block: verdict cards per lane, coverage & requirements table, total cost/time, "How to run" extracted from `plan.commands.run`/README, Iterate form (Task 15).
- [ ] **Step 5:** Docs, CHANGELOG.

**Acceptance:** During a demo implementation the user sees tasks move across the board and a live log; at the end there is one dashboard with everything needed to run, download, or iterate.

---

### Task 19: Panel streaming UX and full-session export

**Model:** Sonnet · **Depends on:** 8, 6

**Files:**
- Modify: `ui/service.py`, `ui/render.py`, `exporter.py`, `ui/view.py`, tests, docs

- [ ] **Step 1:** Failing tests: `build_session_markdown(entries)` includes strategy, blueprint file list, verification lanes, and delivery report (today's exporter ignores everything after the architect); export also offers JSON.
- [ ] **Step 2:** Stream panel tokens: add `"messages"` to `stream_mode`; append tokens to the active bubble so turns appear live instead of after the whole call.
- [ ] **Step 3:** Render the parallel opening statements as a three-column row, and the moderator's convergence note as a slim banner; collapse turns older than the last round by default.
- [ ] **Step 4:** Docs (`README` screenshot refresh happens in Task 23), CHANGELOG.

**Acceptance:** Panel text streams token by token; the export reproduces the whole run.

---

# Phase 4 — Usefulness, quality, polish

### Task 20: Project preferences and model profiles

**Model:** Sonnet · **Depends on:** 5, 16

**Why:** Users can't state constraints up front (stack, platform, deployment target, must-use services), so agents debate and then architect from scratch; and per-role model configuration is 15 loose env vars with no cost/quality presets.

**Files:**
- Modify: `schemas.py` (`ProjectPreferences`), `state.py` (`preferences`), `nodes/*` (single helper `preferences_block(state)` injected into panel/architect/strategy/blueprint prompts), `config.py` (`model_profile: Literal["fast","balanced","quality"]` + `PROFILES` mapping role → `(provider, model)`; explicit `*_MODEL`/`*_PROVIDER` env vars still override), `ui/app.py` (preferences accordion), `.env.example`, docs, tests

**Interfaces (produces):**
```python
class ProjectPreferences(BaseModel):
    platform: Literal["web","mobile","cli","api","any"] = "any"
    stack_hints: str = ""; deploy_target: str = ""; must_use: str = ""; must_avoid: str = ""
def preferences_block(prefs: dict | None) -> str: ...        # '' when all defaults
def resolve_role_model(settings: Settings, role_key: str) -> tuple[Provider, str]: ...
```
- [ ] **Step 1:** Failing tests: `preferences_block` empty for defaults and mentions stack hints otherwise; profile `fast` resolves cheaper models than `quality` for the architect; an explicit `ARCHITECT_MODEL` env overrides the profile.
- [ ] **Step 2:** Implement, wire the accordion (collapsed by default) and show resolved models in the settings accordion.
- [ ] **Step 3:** Blueprint/architect prompts must treat `must_use`/`must_avoid` as hard constraints (assert in a prompt-capture test).
- [ ] **Step 4:** Docs (`.env.example` owns profile docs), CHANGELOG.

**Acceptance:** Stating "TypeScript + Postgres, deploy on Fly.io" visibly changes the architecture options and the blueprint's stack.

---

### Task 21 (optional): Grounded research step

**Model:** Opus · **Depends on:** 5, 8, 20

**Why:** The panel currently debates from model memory. A short, cited market/competitor brief makes the discussion materially more useful.

**Files:**
- Create: `nodes/research.py`, `tests/test_research.py`
- Modify: `schemas.py` (`ResearchBrief`), `state.py`, `graph.py` (`research` before `panel`, skipped unless enabled), `config.py` (`enable_research: bool = False`, `research_provider`), `roles.py`, `demo/fixtures.py`, `.env.example`, docs

**Interfaces (produces):**
```python
class Competitor(BaseModel): name: str; url: str; positioning: str; pricing: str
class ResearchBrief(BaseModel): competitors: list[Competitor]; market_notes: list[str]; gaps: list[str]; sources: list[str]
```
- [ ] **Step 1:** Look up the current provider-native web-search tool definitions with context7 (Anthropic server-side web search, OpenAI web search, Gemini Google Search grounding) — do not rely on memory for tool type names. Implement only the provider configured for `research_provider`.
- [ ] **Step 2:** Failing tests with a fake runtime: `research_node` returns a validated `ResearchBrief`, drops competitors without URLs, is skipped when disabled, and the panel prompt includes the brief.
- [ ] **Step 3:** Render the brief as a card with source links (HTML-escaped, `rel="noopener"`); treat all fetched text as untrusted data (no instructions from it enter tool calls).
- [ ] **Step 4:** Docs, diagram regen, CHANGELOG.

**Acceptance:** With `ENABLE_RESEARCH=true`, the panel references real, linked competitors; disabled by default and fully skippable in demo mode.

---

### Task 22: Evals, coverage gate, CI hardening

**Model:** Sonnet · **Depends on:** 4, 9, 10

**Files:**
- Create: `tests/fixtures/golden_ideas.json`, `tests/test_golden_blueprints.py`, `scripts/eval_blueprint.py`, `.pre-commit-config.yaml`, `.github/dependabot.yml`
- Modify: `pyproject.toml` (`--cov=idea_to_mvp --cov-fail-under=80`, ruff rules, mypy stricter on `llm/`, `schemas.py`, `plan.py`), `.github/workflows/ci.yml`, `CONTRIBUTING.md`

- [ ] **Step 1:** Golden tests (offline, demo fixtures): for 3 golden ideas assert the pack passes `validate_plan`, every P0 requirement is covered, and the DAG has ≥ 2 waves.
- [ ] **Step 2:** `scripts/eval_blueprint.py` — opt-in LLM-as-judge (`--live`) scoring a generated pack on a rubric (requirement coverage, contract consistency, test specificity); prints a table; excluded from CI.
- [ ] **Step 3:** Coverage gate at 80% (raise per-module later), matrix on 3.11–3.13, `uv lock --check`, pre-commit (ruff + ruff-format + mypy), Dependabot for `uv`/`github-actions`.
- [ ] **Step 4:** CONTRIBUTING documents `pytest -m live` and the eval script (single mention).

**Acceptance:** CI fails on coverage < 80%, lock drift, invalid golden packs, or graph-diagram drift.

---

### Task 23: Portfolio polish and docs finalisation

**Model:** Sonnet · **Depends on:** all previous

**Files:** `README.md`, `docs/architecture.md`, `CHANGELOG.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `pyproject.toml`, `docs/assets/*`, delete `docs/plans/`

- [ ] **Step 1:** Record the demo-mode run (browser pane screenshots for stepper, panel, gate forms, implementation board, dashboard; optional GIF) into `docs/assets/`; embed 3–4 in README.
- [ ] **Step 2:** README restructure: pitch → 60-second quick start (`DEMO_MODE=true uv run idea-to-mvp`) → pipeline diagram → "Why it's built this way" (5 bullets) → links (architecture, security, configuration = `.env.example`). Nothing duplicated from `docs/architecture.md`.
- [ ] **Step 3:** `docs/architecture.md`: final graph mermaid (auto), state contract, **"LangGraph patterns used"** section mapping each pattern to file + test (interrupt gates, checkpointer resume, `Send` map-reduce ×4, subgraphs, structured output, evaluator-optimizer loop, retry policies, custom stream events, cancellation), extension guide (add an agent, add a gate).
- [ ] **Step 4:** Version → `0.3.0`, CHANGELOG `## [0.3.0]` with the Unreleased entries consolidated by theme; final pass to delete stale statements across docs (`grep` for removed settings/paths).
- [ ] **Step 5:** Delete `docs/plans/2026-09-29-improvement-plan.md`; final run of the full definition of done.

**Acceptance:** A stranger can clone, run the demo in a minute, understand the LangGraph design from one document, and every doc statement matches the code.

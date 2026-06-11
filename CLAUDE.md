# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then fill in API keys

# Run
python app.py

# Lint
ruff check .

# Test
pytest                                         # all tests
pytest tests/test_agents_routing.py -v         # single file
pytest tests/test_agents_routing.py::test_name # single test
```

## Architecture

**idea-to-mvp** is a Gradio + LangGraph app that takes a product idea through a structured multi-agent pipeline and produces an MVP-ready project blueprint.

### Pipeline Phases

1. **Discussion** — Three agents (PM, Tech Lead, Skeptic) debate the idea in a round-robin loop until `max_rounds` is reached.
2. **Summarizer** — Produces an executive brief and exactly 5 clarifying MVP questions.
3. **Answers gate** — the graph pauses at `collect_answers` (`interrupt()`); user answers resume it.
4. **Architect + architecture gate** — two options; the graph pauses at `arch_choice` for a structured `{option, notes}` decision.
5. **Strategy** — a strategy agent emits strict-JSON `{mode, reasoning, workstreams}` choosing `subagents` vs `agent_team` (fallback strategy on parse failure).
6. **Plan gate + Planner** — pause at `plan_gate`; `{generate, notes}` resumes and creates the blueprint v2 pack in `project_blueprints/`.
7. **Implementation gate + Implementer** — pause at `implement_gate` with a cost warning; `{implement, notes}` resumes. `implementer.py` copies the bundle to `generated_projects/` and drives Claude Agent SDK sessions (lead+subagents or sequential team).
8. **Verifier + Delivery report** — a verification agent runs the generated project's tests (`VERDICT: PASS|FAIL` sentinel) with a bounded fix loop, then a deterministic delivery report ends the run.

### Key Files

| File | Role |
|---|---|
| `app.py` | Gradio UI entrypoint; calls `make_ui()` and `build_graph()` |
| `state.py` | `IdeaDiscussionState` TypedDict — the single shared state contract |
| `graph.py` | Builds the LangGraph `StateGraph` with nodes and conditional routing |
| `agents.py` | All node functions, `LlmRuntime`/`AgentRuntime` dataclasses, LLM invocation logic |
| `roles.py` | `RoleSpec` registry: all system prompts + provider/model/token wiring per agent |
| `blueprints.py` | Blueprint v2 document prompts + `create_project_bundle()` (LLM-free; caller injects `generate_doc`) |
| `implementer.py` | Claude Agent SDK sessions: workspace prep, implementation, verification, fix loop |
| `submit_service.py` | `SubmitService` — stateful generator-based streaming bridge between Gradio and the graph |
| `config.py` | Pydantic `Settings` loaded from `.env`; access via `get_settings()` |
| `render.py` | HTML helpers (`thinking_block`, `turn_block`) for Gradio display |
| `exporter.py` | Builds timestamped Markdown exports saved to `exports/` |
| `text_utils.py` | `normalize_content()` shared across agents and submit service |

### State Machine (graph.py)

```
START → discussion (loop until turn_count ≥ max_rounds)
      → summarizer
      → collect_answers   [interrupt: questions out, answers in]
      → architect
      → arch_choice       [interrupt: options out, {option, notes} in]
      → strategy
      → planner_offer
      → plan_gate         [interrupt: offer out, {generate, notes} in]
      → plan_bundle | END
      → implement_gate    [interrupt: cost warning out, {implement, notes} in]
      → implementer | END
      → verifier (bounded fix loop)
      → delivery_report → END
```

The graph runs on a single checkpointer thread per session; the UI resumes interrupts with `Command(resume=...)`. There is no `phase` field — UI mode mirrors the interrupt the graph is paused at.

### Multi-Provider LLM Abstraction (agents.py)

- `LlmRuntime` / `AgentRuntime` are frozen dataclasses wrapping a LangChain `BaseChatModel`.
- `get_runtime(role_key)` returns a cached runtime for any role in `roles.ROLES` (PM=OpenAI, Tech Lead=Anthropic, Skeptic=Google by default).
- Provider is environment-configured; `_resolve_provider()` auto-detects from model name if there's a mismatch.
- OpenAI models retry with `OPENAI_FALLBACK_MODEL` on empty response; Google models retry when hitting `MAX_TOKENS`.
- Google requires `max_output_tokens` instead of `max_tokens` — handled in `_invoke_with_runtime()`.

### Streaming UI (submit_service.py)

`SubmitService.handle_submit()` is a Python generator that yields `gr.update()` calls as the graph streams. The graph is run with `stream_mode="updates"`, producing node-level events consumed by the service and forwarded to Gradio components.

### Project Blueprint Generation (agents.py)

When the user accepts the planner offer, five files are LLM-generated into a timestamped `project_blueprints/` subdirectory:
- `AGENTS.md` (root project guide)
- `contracts/AGENTS.md`, `application/AGENTS.md`, `quality/AGENTS.md`
- `plan.md` (execution task list with contract registry and test specs)

Each file has its own system prompt tuned for agent consumption.

### Import Pattern

All modules use try/except dual imports to support both CLI and pytest execution:

```python
try:
    from .config import get_settings
except ImportError:
    from config import get_settings
```

### Test Isolation

`config.py` exports `clear_settings_cache()` — call it in test teardown to reset the `lru_cache` between tests. `conftest.py` sets up the Python path so tests can import top-level modules.

Graph lifecycle tests fake all LLM/SDK calls by monkeypatching `agents.get_runtime`, `agents._invoke_with_runtime`, and the implementer functions re-exported on `agents` (`prepare_workspace`, `run_implementation`, `run_verification`, `run_fix`) plus `agents._project_bundle_root` — see `tests/test_agents_routing.py`.

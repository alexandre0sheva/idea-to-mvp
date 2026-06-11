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
3. **Answers** — User responds to the questions; answers flow into the next phase.
4. **Architect** — Generates two implementation options (fast/simple vs. performant/scalable).
5. **Planner** — Optionally creates a `project_blueprints/` folder with AGENTS.md files and a `plan.md`.

### Key Files

| File | Role |
|---|---|
| `app.py` | Gradio UI entrypoint; calls `make_ui()` and `build_graph()` |
| `state.py` | `IdeaDiscussionState` TypedDict — the single shared state contract |
| `graph.py` | Builds the LangGraph `StateGraph` with nodes and conditional routing |
| `agents.py` | All node functions, system prompts, `LlmRuntime`/`AgentRuntime` dataclasses, LLM invocation logic |
| `submit_service.py` | `SubmitService` — stateful generator-based streaming bridge between Gradio and the graph |
| `config.py` | Pydantic `Settings` loaded from `.env`; access via `get_settings()` |
| `render.py` | HTML helpers (`thinking_block`, `turn_block`) for Gradio display |
| `exporter.py` | Builds timestamped Markdown exports saved to `exports/` |
| `text_utils.py` | `normalize_content()` shared across agents and submit service |

### State Machine (graph.py)

```
START → route_from_start()
          ├─ discussion (loop) → route_after_discussion() → discussion | summarizer → END
          ├─ architect → planner_offer → END
          └─ plan_bundle → END
```

Routing is driven by `state["phase"]` (`"idea"`, `"answers"`, `"plan_bundle"`) and `state["turn_count"]` vs `state["max_rounds"]`.

### Multi-Provider LLM Abstraction (agents.py)

- `LlmRuntime` / `AgentRuntime` are frozen dataclasses wrapping a LangChain `BaseChatModel`.
- `get_discussion_runtimes()` returns LRU-cached runtimes for PM (OpenAI), Tech Lead (Anthropic), and Skeptic (Google).
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
